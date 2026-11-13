"""Automation around money arriving: auto-continue held lines, ask approvers, remind, report outcomes.

Hard rules (this module only PREPARES work for humans):

* It never approves, signs or moves money. A continuation is created through the same service path as
  ``POST /runs/{id}/pay-held`` (:meth:`PlatformRuntime.start_continuation`): it is priced and then waits in
  RESERVED for a Visa TAP-signed human decision.
* At most one automatic continuation per parent run: a parent that already has ANY continuation (manual or
  automatic, whatever its outcome) is left to the humans. The runtime's own guard (a later payroll already
  paid those contractors, an open run, inactive employer...) is reused, never re-implemented.
* Everything is tenant-scoped by the organization passed in; emails are queued (delivery is async).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..crew import funding
from ..db.models import Employer, Membership, Notification, Organization, PayrollRun, User
from ..money import money_str
from ..policy import PAYROLL_POLICY, ContractorPayroll, RefusalError, assess_payroll
from . import email_templates as tpl
from . import notify
from .runtime import CONTINUABLE, HeldLineUnavailable, PlatformRuntime, RuntimeConflict

logger = logging.getLogger(__name__)

APPROVER_ROLES = ("owner", "admin", "approver")
HELD_REMINDER_AFTER = timedelta(hours=24)
OUTCOME_LOOKBACK = timedelta(days=3)


def run_url(settings: Settings, run_id) -> str:
    return f"{settings.app_base_url.rstrip('/')}/dashboard/runs/{run_id}"


def run_label(run: PayrollRun) -> str:
    return run.period_label or f"run {str(run.id)[:8]}"


def approvers(db: Session, org: Organization) -> list[User]:
    rows = db.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id)
                      .where(Membership.organization_id == org.id, Membership.role.in_(APPROVER_ROLES),
                             User.is_active.is_(True)).order_by(Membership.created_at)).all()
    return [u for u, _ in rows]


def _actor(db: Session, org: Organization) -> User | None:
    """The user a system-created run is attributed to: an owner (never an approver of it by this code)."""
    return db.scalar(select(User).join(Membership, Membership.user_id == User.id)
                     .where(Membership.organization_id == org.id, Membership.role == "owner",
                            User.is_active.is_(True)).order_by(Membership.created_at))


def wallet_covers_held(db: Session, runtime: PlatformRuntime, parent: PayrollRun) -> bool:
    """Can the employer's OWN wallet fund the held lines (+ run fee unless waived) and keep the reserve?"""
    held = runtime.held_pay_lines(parent)
    if not held:
        return False
    employer = parent.employer
    wallet = {k: Decimal(v) for k, v in runtime.balances(db, employer).items()}
    try:
        rates = funding.usd_rates_for(runtime, employer, {pl.currency for pl in held} | set(wallet))
        a = assess_payroll([ContractorPayroll(pl.beneficiary_name, pl.amount, pl.currency, pl.method) for pl in held],
                           rates, wallet, PAYROLL_POLICY)
    except RefusalError:
        return False
    fee = Decimal("0") if runtime.run_fee_collected(db, parent) else a.platform_fee_usd
    return a.available_usd >= a.payroll_usd + fee + a.reserve_floor_usd


def _continuable_parents(db: Session, employer: Employer) -> list[PayrollRun]:
    return list(db.scalars(select(PayrollRun).options(selectinload(PayrollRun.pay_lines))
                           .where(PayrollRun.employer_id == employer.id,
                                  PayrollRun.organization_id == employer.organization_id,
                                  PayrollRun.status.in_(CONTINUABLE))
                           .order_by(PayrollRun.created_at.desc())))


def request_approval(db: Session, settings: Settings, org: Organization, employer: Employer,
                     run: PayrollRun, *, continuation: bool) -> int:
    """Queue one approval-request email per approver (deduped per run + person). Returns how many were new."""
    from . import payroll_mail  # lazy: it imports this module

    return payroll_mail.request_approval(db, settings, org, run)


def continue_held_for(db: Session, runtime: PlatformRuntime, settings: Settings, org: Organization,
                      employer: Employer, parent: PayrollRun, actor: User | None = None) -> dict:
    """The ONE guarded path that prepares a continuation for one parent run (autopilot and workflows share it).

    Idempotent: a parent that already has ANY continuation is never given a second one; the existing run is
    reported instead (``action="existing"``). Creates only; never approves, signs or pays.
    Returns ``{"action": "created"|"existing"|"skipped", "reason": str, "run_id": str|None}``.
    """
    existing = runtime.continuations_of(db, parent)
    if existing:
        return {"action": "existing", "run_id": str(existing[0].id),
                "reason": "Held lines already have a continuation run; a person decides from here."}
    blocker = runtime.pay_held_blocker(db, parent)
    if blocker:
        return {"action": "skipped", "reason": blocker, "run_id": None}
    if not wallet_covers_held(db, runtime, parent):
        return {"action": "skipped", "reason": "The wallet does not cover the held lines yet.", "run_id": None}
    actor = actor or _actor(db, org)
    if actor is None:
        return {"action": "skipped", "reason": "No organization owner to attribute the run to.", "run_id": None}
    try:
        run = runtime.start_continuation(db, org=org, user=actor, run=parent)
    except (RuntimeConflict, HeldLineUnavailable) as exc:
        db.rollback()
        return {"action": "skipped", "reason": str(exc), "run_id": None}
    db.commit()
    if run.status == "RESERVED":  # paused at the approval step: ask the humans who can decide
        request_approval(db, settings, org, employer, run, continuation=True)
        db.commit()
        return {"action": "created", "run_id": str(run.id),
                "reason": "Continuation created and waiting for approval; approvers were emailed."}
    return {"action": "created", "run_id": str(run.id),
            "reason": f"Continuation created but it is {run.status}; no approval is pending."}


def auto_continue(db: Session, runtime: PlatformRuntime, settings: Settings, org: Organization,
                  employer: Employer) -> dict:
    """If the employer's wallet can now cover the held lines of a PARTIALLY_PAID run, prepare the continuation.

    Returns ``{"action": "created"|"skipped", "reason": str, "run_id": str|None}``.
    """
    result: dict = {"action": "skipped", "reason": "No held lines waiting.", "run_id": None}
    if not settings.auto_continue_held:
        return {**result, "reason": "Automatic continuation is switched off (AUTO_CONTINUE_HELD)."}
    parents = _continuable_parents(db, employer)
    if not parents:
        return result
    actor = _actor(db, org)
    if actor is None:
        return {**result, "reason": "No organization owner to attribute the run to."}
    for parent in parents:
        outcome = continue_held_for(db, runtime, settings, org, employer, parent, actor)
        if outcome["action"] == "created":
            return outcome
        result["reason"] = outcome["reason"]
    return result


def _emit_funds_received(db: Session, runtime: PlatformRuntime, org: Organization, employer: Employer,
                         amount: str, outcome: dict, reference: str | None) -> None:
    """Fire the ``funds.received`` workflow event (after any automatic continuation exists, so a workflow
    step can link to it). Guarded: a workflow problem never hides the deposit."""
    try:
        from .workflows import dispatch_event

        dispatch_event(
            db, organization_id=org.id, event_type="funds.received", entity_kind="employer",
            entity_id=str(employer.id), runtime=runtime,
            dedupe_suffix=reference or f"{amount}:{datetime.now(UTC).isoformat()}",
            facts={"employer_id": str(employer.id), "employer_name": employer.name, "amount": amount,
                   "currency": employer.funding_currency, "contact_email": employer.contact_email,
                   "continuation_run_id": outcome.get("run_id") if outcome.get("action") == "created" else None})
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Workflow funds.received dispatch failed for employer %s (ignored)", employer.id)
        db.rollback()


def on_funds_arrived(db: Session, runtime: PlatformRuntime, settings: Settings, org: Organization,
                     employer: Employer, amount: str, reference: str | None = None) -> dict:
    """Hook after a deposit lands: tell the employer, then auto-continue held lines if now affordable.

    Never raises: a failure here must not undo or hide the deposit.
    """
    try:
        outcome = auto_continue(db, runtime, settings, org, employer)
        _emit_funds_received(db, runtime, org, employer, amount, outcome, reference)
        if employer.contact_email:
            subject, text, html = tpl.funds_received(employer.name, tpl.money("USD", amount), org.name,
                                                     continuation_started=outcome["action"] == "created")
            notify.queue_email(db, org, "FUNDS_RECEIVED", employer.contact_email, subject, text, body_html=html,
                               employer_id=employer.id, meta={"amount_usd": amount})
        db.commit()
        return outcome
    except Exception:  # noqa: BLE001
        logger.exception("Auto-continuation after funding failed for employer %s", employer.id)
        db.rollback()
        return {"action": "skipped", "reason": "Automation failed; use Pay held lines on the run.", "run_id": None}


def remind_held_lines(db: Session, runtime: PlatformRuntime, settings: Settings, org: Organization,
                      employer: Employer, now: datetime | None = None) -> int:
    """Daily nudge to approvers about held lines nobody continued for over 24 hours."""
    now = now or datetime.now(UTC)
    queued = 0
    for parent in _continuable_parents(db, employer):
        created = parent.updated_at if parent.updated_at.tzinfo else parent.updated_at.replace(tzinfo=UTC)
        if now - created < HELD_REMINDER_AFTER or runtime.continuations_of(db, parent):
            continue
        funded = wallet_covers_held(db, runtime, parent)
        for user in approvers(db, org):
            subject, text, html = tpl.held_lines_reminder(employer.name, run_label(parent),
                                                          len(runtime.held_pay_lines(parent)),
                                                          run_url(settings, parent.id), org.name, funded=funded)
            key = f"heldrem:{parent.id}:{user.id}:{now.date().isoformat()}"
            before = db.scalar(select(Notification.id).where(
                Notification.organization_id == org.id, Notification.dedupe_key == key))
            notify.queue_email(db, org, "HELD_LINES_REMINDER", user.email, subject, text, body_html=html,
                               employer_id=employer.id, dedupe_key=key, meta={"run_id": str(parent.id)})
            queued += before is None
    return queued


def report_run_outcome(db: Session, settings: Settings, org: Organization, run: PayrollRun) -> int:
    """Queue the SETTLED / FAILED notice for one run: one per run and person (shared dedupe key with the sweep)."""
    if run.status not in ("SETTLED", "FAILED"):
        return 0
    from . import payroll_mail  # lazy: it imports this module

    return payroll_mail.announce_run(db, settings, org, run, None)


def report_outcomes(db: Session, settings: Settings, org: Organization, now: datetime | None = None) -> int:
    """Tell approvers about recently SETTLED / FAILED runs, once per run and person."""
    now = now or datetime.now(UTC)
    runs = db.scalars(select(PayrollRun).options(selectinload(PayrollRun.employer))
                      .where(PayrollRun.organization_id == org.id, PayrollRun.status.in_(("SETTLED", "FAILED")),
                             PayrollRun.updated_at >= now - OUTCOME_LOOKBACK)).all()
    return sum(report_run_outcome(db, settings, org, run) for run in runs)
