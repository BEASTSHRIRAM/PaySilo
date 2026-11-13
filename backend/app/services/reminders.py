"""Funding reminders: build, queue and deliver funding emails, and the org-wide sweep.

The sweep is deterministic: an employer is reminded when it is short and either today is 3, 2, 1 or 0
business days before ``fund_by`` (one email per offset), or it is OVERDUE (one email per day). The dedupe
key ``funding:{employer_id}:{pay_date}:{offset}`` makes a second sweep a no-op, so running it twice -
by hand, by the scheduler, or from two places - never double-sends.

The scheduler (``funding_reminder_loop``) assumes ONE worker process; with several workers the dedupe key
still prevents duplicate emails, but each worker would repeat the sweep work.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..crew import draft, funding
from ..db.models import Employer, Notification, Organization
from . import autopilot, notify
from . import email_templates as tpl

logger = logging.getLogger(__name__)

REMINDER_OFFSETS = (3, 2, 1, 0)
SWEEP_INTERVAL_SECONDS = 30 * 60
FIRST_SWEEP_DELAY_SECONDS = 60
DELIVERY_INTERVAL_SECONDS = 60


def send_funding_email(db: Session, org: Organization, employer: Employer, plan: dict, kind: str,
                       settings: Settings, *, dedupe_key: str | None = None, meta: dict | None = None) -> Notification:
    """Draft (template + optional guarded polish) and QUEUE one email to the employer's contact.

    Delivery is asynchronous (:func:`notify.deliver_pending`); this never blocks on a mail server.
    """
    subject, body = draft.draft_funding_email(kind, plan, org.name)
    body, polished = draft.polish_body(body, plan, settings=settings)
    return notify.queue_email(
        db, org, kind, employer.contact_email or "", subject, body, body_html=tpl.render_html(body, org=org.name, reason=tpl.REASON_EMPLOYER, subject=subject),
        employer_id=employer.id, dedupe_key=dedupe_key,
        meta={**(meta or {}), "pay_date": plan["pay_date"], "fund_by": plan["fund_by"], "gap_usd": plan["gap_usd"],
              "recommended_transfer_usd": plan["recommended_transfer_usd"], "polished": polished})


def reminder_offset(plan: dict, today: date) -> int | None:
    if not plan.get("fund_by"):
        return None
    return funding.business_days_between(today, date.fromisoformat(plan["fund_by"]))


def sweep_organization(db: Session, runtime, settings: Settings, org: Organization, today: date | None = None
                       ) -> list[dict]:
    """Send due reminders for one organization. Returns one result row per employer (what and why)."""
    today = today or date.today()
    results: list[dict] = []
    employers = db.scalars(select(Employer).where(Employer.organization_id == org.id)
                           .order_by(Employer.created_at)).all()
    for emp in employers:
        emp = db.scalar(select(Employer).options(selectinload(Employer.contractors)).where(Employer.id == emp.id))
        result = {"employer_id": str(emp.id), "employer_name": emp.name, "action": "skipped", "reason": "",
                  "notification_id": None, "status": None}
        results.append(result)
        try:
            auto = autopilot.auto_continue(db, runtime, settings, org, emp)
            if auto["action"] == "created":
                result["auto_continuation"] = auto
            autopilot.remind_held_lines(db, runtime, settings, org, emp)
            db.commit()
            plan = funding.plan_employer(db, runtime, emp, today, with_wire=True).as_dict()
            if plan["status"] in ("RATE_UNAVAILABLE",):
                result["reason"] = "Amounts could not be priced right now."
                continue
            if not plan["is_short"]:
                result["reason"] = {"NO_LINES": "Nothing to fund yet.",
                                    "NO_SCHEDULE": "No pay date set."}.get(plan["status"], "Already fully funded.")
                continue
            if plan["status"] == "NO_SCHEDULE":
                result["reason"] = "No pay date set."
                continue
            if not emp.contact_email:
                result["reason"] = "No contact email on file."
                continue
            overdue = plan["status"] == "OVERDUE"
            offset = reminder_offset(plan, today)
            if overdue:
                label, kind = f"overdue-{today.isoformat()}", "FUNDING_OVERDUE"
            elif offset in REMINDER_OFFSETS:
                label, kind = str(offset), "FUNDING_REMINDER"
            else:
                result["reason"] = "Not yet in the reminder window (3 business days before fund-by)."
                continue
            key = f"funding:{emp.id}:{plan['pay_date']}:{label}"
            existing = db.scalar(select(Notification).where(Notification.organization_id == org.id,
                                                            Notification.dedupe_key == key))
            if existing is not None:
                result.update(reason=("Delivery failed earlier; see Notifications." if existing.status == "FAILED"
                                      else "Already sent for this step."), notification_id=str(existing.id),
                              status=existing.status)
                continue
            row = send_funding_email(db, org, emp, plan, kind, settings, dedupe_key=key,
                                     meta={"offset": label, "source": "sweep"})
            result.update(action="queued", reason=("Overdue: daily reminder." if overdue else
                                                   f"{offset} business day(s) before fund-by."),
                          notification_id=str(row.id), status=row.status)
        except Exception as exc:  # noqa: BLE001 - one employer must not stop the sweep
            logger.exception("Funding sweep failed for employer %s", emp.id)
            db.rollback()
            result["reason"] = f"Could not check this employer ({type(exc).__name__})."
    try:
        autopilot.report_outcomes(db, settings, org)
    except Exception:  # noqa: BLE001
        logger.exception("Reporting run outcomes failed for organization %s", org.id)
        db.rollback()
    try:
        from . import payroll_mail

        payroll_mail.remind_waiting_approvals(db, settings, org)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Approval reminders failed for organization %s", org.id)
        db.rollback()
    try:
        from . import compliance_mail

        compliance_mail.tax_form_alert(db, settings, org, today)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Tax form alert failed for organization %s", org.id)
        db.rollback()
    db.commit()
    return results


def sweep_all(db_factory, runtime, settings: Settings, today: date | None = None) -> int:
    """Sweep every organization; error-safe. Returns the number of emails queued."""
    queued = 0
    with db_factory() as s:
        org_ids = [o for o in s.scalars(select(Organization.id))]
    for org_id in org_ids:
        try:
            with db_factory() as s:
                org = s.get(Organization, org_id)
                queued += sum(1 for r in sweep_organization(s, runtime, settings, org, today)
                              if r["action"] == "queued")
        except Exception:  # noqa: BLE001
            logger.exception("Funding sweep failed for organization %s", org_id)
        try:  # scheduled workflows (once per period per entity: their dedupe key makes a repeat a no-op)
            from datetime import UTC, datetime

            from .workflows import run_due_schedules

            with db_factory() as s:
                run_due_schedules(s, now=datetime.now(UTC), runtime=runtime, organization_id=org_id)
                s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Scheduled workflows failed for organization %s", org_id)
    return queued


async def funding_reminder_loop(db_factory, runtime, settings: Settings) -> None:
    """Every 30 minutes: run the sweep for all organizations. Never raises, never stops on errors."""
    await asyncio.sleep(FIRST_SWEEP_DELAY_SECONDS)
    while True:
        try:
            queued = await asyncio.to_thread(sweep_all, db_factory, runtime, settings)
            if queued:
                logger.info("Funding reminder sweep queued %d email(s).", queued)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Funding reminder sweep crashed; will retry next interval.")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def email_delivery_loop(db_factory, settings: Settings) -> None:
    """Every minute: send queued emails and retry failed ones (with backoff). Idle when email is not configured."""
    mailer = notify.mailer_for(settings)
    while True:
        try:
            if mailer.configured:
                await asyncio.to_thread(notify.deliver_pending, db_factory, mailer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Email delivery loop error; continuing.")
        await asyncio.sleep(DELIVERY_INTERVAL_SECONDS)
