"""Payroll lifecycle emails, sent from the one place a run's persisted status changes.

``announce_run`` is called by the runtime after every graph step is persisted. It only QUEUES mail (outbox,
async delivery): it never approves, signs or moves money, and a mail problem never affects a run.

Every mail has a dedupe key made of the run and the person, so a repeat call, a sweep and a workflow node can
never send the same mail twice. The keys are shared on purpose: ``approval:{run}:{user}`` is also used by the
autopilot continuation and by the workflow 'approval requested' step; ``wf:payslip:{line}`` is also used by the
workflow payslip step.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Approval, Contractor, Notification, Organization, PayLine, PayrollRun, User
from . import autopilot, notify
from . import email_templates as tpl
from .email_layout import money
from .payment_trace import FAILURE_CODES, explain_run_error, extract_failure_code

logger = logging.getLogger(__name__)

MAX_APPROVAL_REMINDERS = 3
REJECTED_STATUSES = ("HELD_BY_HUMAN", "APPROVAL_REJECTED")
PAID_STATUSES = ("SETTLED", "PARTIALLY_PAID")


def _usd(value) -> str:
    return money("USD", value) if value is not None else ""


def _queue(db: Session, org: Organization, template: str, to: str, ctx: dict, *, key: str, run: PayrollRun,
           contractor_id=None, kind: str | None = None) -> bool:
    """Queue one mail from the registry. True when it is new (not a repeat of an earlier one)."""
    before = db.scalar(select(Notification.id).where(Notification.organization_id == org.id,
                                                     Notification.dedupe_key == key))
    spec = tpl.TEMPLATES[template]
    subject, text, html = spec.build(ctx, org.name)
    notify.queue_email(db, org, kind or spec.kind, to, subject, text, body_html=html, employer_id=run.employer_id,
                       contractor_id=contractor_id, dedupe_key=key, meta={"run_id": str(run.id), "template": template},
                       sensitive=spec.sensitive)
    return before is None


def run_context(settings: Settings, run: PayrollRun) -> dict:
    employer = run.employer
    held = [{"name": pl.beneficiary_name, "amount": str(pl.amount), "currency": pl.currency,
             "reason": pl.hold_reason} for pl in run.pay_lines if pl.status == "HELD"]
    return {"employer_name": employer.name if employer else "", "run_label": autopilot.run_label(run),
            "total": _usd(run.required_usd) if run.required_usd is not None else "the assessed total",
            "url": autopilot.run_url(settings, run.id), "held": held,
            "pays": sum(1 for pl in run.pay_lines if pl.status != "HELD"),
            "settlement_mode": getattr(employer, "settlement_mode", None), "currency": "USD",
            "total_required": str(run.required_usd) if run.required_usd is not None else "",
            "shortfall": str(run.shortfall_usd) if run.shortfall_usd is not None else ""}


def _decider(db: Session, run: PayrollRun, decision: str | None = None) -> str:
    stmt = select(Approval).where(Approval.run_id == run.id).order_by(Approval.created_at.desc())
    for approval in db.scalars(stmt):
        if decision is None or approval.decision == decision:
            user = db.get(User, approval.decided_by_id) if approval.decided_by_id else None
            return (user.full_name if user and user.full_name else approval.decided_by_email) or "An approver"
    return "An approver"


def request_approval(db: Session, settings: Settings, org: Organization, run: PayrollRun) -> int:
    """Run prepared and waiting: one mail per approver. Same key as the autopilot continuation mail."""
    ctx = run_context(settings, run)
    ctx.update(continuation=bool(run.parent_run_id), held=len(ctx["held"]))
    return sum(_queue(db, org, "approval_requested", u.email, ctx, key=f"approval:{run.id}:{u.id}", run=run)
               for u in autopilot.approvers(db, org))


def _plain_reason(run: PayrollRun) -> str:
    code = extract_failure_code(run.error)
    if code in FAILURE_CODES:
        return FAILURE_CODES[code][0] + ". " + FAILURE_CODES[code][1]
    exp = explain_run_error(run.error)
    return exp.message + (f" {exp.hint}" if exp.hint else "") if exp else "The run stopped before every payment finished."


def payslips(db: Session, settings: Settings, org: Organization, run: PayrollRun) -> int:
    """A SENSITIVE payment notice for every paid line whose contractor has an email (private receipt link)."""
    from ..api.routes.bedata import receipt_token  # lazy: routes import services

    base = settings.app_base_url.rstrip("/")
    sent = 0
    for line in run.pay_lines:
        if line.status != "PAID" or line.contractor_id is None:
            continue
        contractor = db.get(Contractor, line.contractor_id)
        if contractor is None or contractor.organization_id != org.id or not contractor.email:
            continue
        token = receipt_token(settings, line.id)
        ctx = {"contractor_name": line.beneficiary_name, "employer_name": run.employer.name,
               "amount": str(line.amount), "currency": line.currency,
               "receipt_link": f"{base}/receipt/{line.id}?token={token}",
               "proof_link": f"{base}/api/receipts/{line.id}/proof.pdf?token={token}"}
        sent += _queue(db, org, "payslip_receipt", contractor.email, ctx, key=f"wf:payslip:{line.id}", run=run,
                       contractor_id=contractor.id)
    return sent


def announce_run(db: Session, settings: Settings, org: Organization, run: PayrollRun, prev_status: str | None) -> int:
    """Queue whatever mail this status deserves. Returns the number of NEW mails. Caller commits."""
    status, new = run.status, 0
    people = autopilot.approvers(db, org)
    ctx = run_context(settings, run)
    pending_kind = (run.pending_interrupt or {}).get("kind") if isinstance(run.pending_interrupt, dict) else None

    if status == "RESERVED" and pending_kind == "approval":
        new += request_approval(db, settings, org, run)
    if status == "HELD_SHORTFALL" or (status == "RESERVED" and pending_kind == "escalation"):
        new += sum(_queue(db, org, "run_held", u.email, ctx, key=f"held:{run.id}:{u.id}", run=run) for u in people)
    if status == "PARTIALLY_PAID":
        held = ctx["held"]
        paid_total = sum(float(pl.amount) for pl in run.pay_lines if pl.status == "PAID" and pl.currency == "USD")
        fund = ctx["shortfall"] or ""
        pctx = {**ctx, "paid_total": money("USD", f"{paid_total:.2f}") if paid_total else "see the run",
                "shortfall": _usd(run.shortfall_usd) if run.shortfall_usd is not None else "the held amount",
                "fund_amount": _usd(run.shortfall_usd) if fund else "see the run"}
        new += sum(_queue(db, org, "run_partial", u.email, pctx, key=f"partial:{run.id}:{u.id}", run=run)
                   for u in people)
    risk = [h for h in ctx["held"] if h.get("reason")]
    if risk and status in ("RESERVED", "PARTIALLY_PAID", "HELD_SHORTFALL", "HELD_BY_HUMAN"):
        rctx = {**ctx, "held": risk}
        new += sum(_queue(db, org, "risk_hold", u.email, rctx, key=f"risk:{run.id}:{u.id}", run=run) for u in people)
    if status in REJECTED_STATUSES:
        rejecter = _decider(db, run)
        recipients = {u.email: u for u in people}
        starter = db.get(User, run.created_by_id) if run.created_by_id else None
        if starter is not None and starter.is_active:
            recipients.setdefault(starter.email, starter)
        for u in recipients.values():
            new += _queue(db, org, "run_rejected", u.email, {**ctx, "approver": rejecter},
                          key=f"rejected:{run.id}:{u.id}", run=run)
    if status == "FAILED":
        for u in people:
            new += _queue(db, org, "payment_failed", u.email,
                          {**ctx, "reason": _plain_reason(run)}, key=f"outcome:{run.id}:FAILED:{u.id}", run=run)
    if status == "SETTLED":
        for u in people:
            new += _queue(db, org, "run_settled", u.email, ctx, key=f"outcome:{run.id}:SETTLED:{u.id}", run=run)
    if status in PAID_STATUSES and settings.notify_contractors:
        new += payslips(db, settings, org, run)
    if (settings.notify_run_approved and prev_status == "RESERVED" and status not in ("RESERVED", *REJECTED_STATUSES)
            and run.created_by_id):
        starter = db.get(User, run.created_by_id)
        approver = _decider(db, run, "approve")
        if starter is not None and starter.is_active and approver != (starter.full_name or starter.email):
            new += _queue(db, org, "run_approved", starter.email,
                          {**ctx, "approver": approver, "name": (starter.full_name or "").split(" ")[0] or None},
                          key=f"approved:{run.id}:{starter.id}", run=run)
    return new


def remind_waiting_approvals(db: Session, settings: Settings, org: Organization, now: datetime | None = None) -> int:
    """A run still waiting after N hours (APPROVAL_REMINDER_HOURS) reminds approvers: at most 3 reminders."""
    hours = max(1, int(settings.approval_reminder_hours))
    now = now or datetime.now(UTC)
    queued = 0
    runs = db.scalars(select(PayrollRun).where(PayrollRun.organization_id == org.id, PayrollRun.status == "RESERVED")).all()
    for run in runs:
        if not (isinstance(run.pending_interrupt, dict) and run.pending_interrupt.get("kind") == "approval"):
            continue
        since = run.updated_at if run.updated_at.tzinfo else run.updated_at.replace(tzinfo=UTC)
        number = min(MAX_APPROVAL_REMINDERS, int((now - since) / timedelta(hours=hours)))
        if number < 1:
            continue
        ctx = {**run_context(settings, run), "hours": number * hours, "number": number, "max": MAX_APPROVAL_REMINDERS}
        for u in autopilot.approvers(db, org):
            ctx["name"] = (u.full_name or "").split(" ")[0] or None
            queued += _queue(db, org, "approval_reminder", u.email, dict(ctx),
                             key=f"apprem:{run.id}:{number}:{u.id}", run=run)
    return queued
