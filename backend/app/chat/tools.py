"""Silo chat tools.

Every tool takes its organization context from :class:`ChatContext`, which
the chat service builds from the authenticated session — never from model
input or tool arguments. Tool arguments can only *name* things inside the
caller's own organization; every query filters by that organization id,
so a tool structurally cannot see another organization's employers, runs
or wallets, and cannot move money across employer silos either.

There is deliberately **no** approve or transfer tool. The action tools
can start a run (which pauses at the approval interrupt) and *request* an
approval (which surfaces the pending decision to a human). Money moves
only on the TAP-signed human decision made on the run itself.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db.models import Employer, PayrollRun
from ..money import money_str, to_decimal
from ..services.runtime import CONTINUABLE, HeldLineUnavailable, PlatformRuntime, RuntimeConflict

EDITOR_ROLES = {"owner", "admin"}


@dataclass
class ChatContext:
    """Server-side context for one chat request. Built from the session only."""

    db: Session
    runtime: PlatformRuntime
    organization_id: uuid.UUID
    organization_name: str
    user_id: uuid.UUID
    user_email: str
    role: str
    demo_mode: bool
    page_employer_id: str | None = None  # UI context, validated against the org before use
    page_run_id: str | None = None


@dataclass
class ToolResult:
    text: str
    approval: dict | None = None  # {run_id, employer_id, employer_name, summary} when a run awaits a human
    data: dict = field(default_factory=dict)


def fmt_usd(value: object) -> str:
    return f"USD {to_decimal(str(value)):,.2f}"


# ---------------------------------------------------------------------------
# Resolution helpers (org-scoped by construction)
# ---------------------------------------------------------------------------

def employers(ctx: ChatContext) -> list[Employer]:
    return list(ctx.db.scalars(
        select(Employer).options(selectinload(Employer.contractors))
        .where(Employer.organization_id == ctx.organization_id)
        .order_by(Employer.created_at)))


def employer_candidates(ctx: ChatContext, ref: str | None) -> list[Employer]:
    """Employers in the caller's org that ``ref`` names EXACTLY.

    An id, or a full name (case-insensitive), is an exact reference. The
    employer's first word ("Acme") is accepted only when it is unique in
    the org. Substrings never match ("a" is not "Acme"). More than one
    candidate means the reference is ambiguous.
    """
    rows = employers(ctx)
    if not ref:
        return []
    ref_clean = ref.strip()
    if not ref_clean:
        return []
    try:
        emp_id = uuid.UUID(ref_clean)
    except ValueError:
        emp_id = None
    if emp_id is not None:
        return [e for e in rows if e.id == emp_id]
    lowered = ref_clean.casefold()
    exact = [e for e in rows if e.name.casefold() == lowered]
    if exact:
        return exact
    return [e for e in rows if e.name.split()[0].casefold() == lowered]


def resolve_employer(ctx: ChatContext, ref: str | None) -> Employer | None:
    """Resolve an employer by id or exact name inside the caller's org; None if absent or ambiguous."""
    if ref:
        found = employer_candidates(ctx, ref)
        return found[0] if len(found) == 1 else None
    if ctx.page_employer_id:
        return resolve_employer(ctx, ctx.page_employer_id)
    return None


def employer_not_resolved(ctx: ChatContext, ref: str | None) -> ToolResult:
    """The reply when ``ref`` named no employer, or several (ask, never guess)."""
    found = employer_candidates(ctx, ref)
    if len(found) > 1:
        names = ", ".join(e.name for e in found)
        return ToolResult(f"'{ref}' could mean more than one employer ({names}). "
                          "Which one do you mean? Give me the full name.",
                          data={"employer": ref, "found": False, "ambiguous": [e.name for e in found]})
    return ToolResult(f"I cannot find an employer named '{ref}' in {ctx.organization_name}.",
                      data={"employer": ref, "found": False})


def resolve_run(ctx: ChatContext, run_ref: str | None = None, employer: Employer | None = None) -> PayrollRun | None:
    ref = run_ref or ctx.page_run_id
    q = (select(PayrollRun).options(selectinload(PayrollRun.employer), selectinload(PayrollRun.pay_lines),
                                    selectinload(PayrollRun.events))
         .where(PayrollRun.organization_id == ctx.organization_id))
    if ref:
        try:
            run_id = uuid.UUID(str(ref))
        except ValueError:
            return None
        return ctx.db.scalar(q.where(PayrollRun.id == run_id))
    if employer is not None:
        q = q.where(PayrollRun.employer_id == employer.id)
    return ctx.db.scalar(q.order_by(PayrollRun.created_at.desc()))


def approval_payload(run: PayrollRun) -> dict:
    a = run.assessment or {}
    if run.pending_interrupt and run.pending_interrupt.get("kind") == "escalation":
        summary = (f"{run.employer.name} is held: payroll is short by {fmt_usd(a.get('shortfall_usd', '0'))}. "
                   "It can only be held — fund the employer's own wallet, then start a new run.")
    else:
        fee = ("no run fee: already collected on the original payroll" if a.get("run_fee_waived")
               else f"including the {fmt_usd(a.get('platform_fee_usd', '149'))} run fee")
        kind = "continuation payroll (held lines only)" if run.parent_run_id else "payroll"
        summary = (f"{run.employer.name} {kind} of {fmt_usd(a.get('required_usd', '0'))} ({fee}) is ready and "
                   "waiting for a TAP-signed human approval. Approving happens on the run, not in chat.")
    return {"run_id": str(run.id), "employer_id": str(run.employer_id),
            "employer_name": run.employer.name, "summary": summary}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def list_employers(ctx: ChatContext) -> ToolResult:
    rows = employers(ctx)
    if not rows:
        return ToolResult("This organization has no employers yet.")
    lines = [f"- {e.name} ({e.status})" for e in rows]
    return ToolResult(f"Employers in {ctx.organization_name}:\n" + "\n".join(lines),
                      data={"employers": [{"id": str(e.id), "name": e.name, "status": e.status}
                                          for e in rows]})


def get_employer_balance(ctx: ChatContext, employer: str | None = None) -> ToolResult:
    emp = resolve_employer(ctx, employer)
    if employer and emp is None:
        return employer_not_resolved(ctx, employer)
    targets = [emp] if emp else employers(ctx)
    parts = []
    balances_by_employer: dict[str, dict[str, str]] = {}
    for e in targets:
        balances = ctx.runtime.balances(ctx.db, e)
        balances_by_employer[e.name] = dict(balances)
        rendered = ", ".join(f"{ccy} {amt}" for ccy, amt in balances.items()) or "no funds yet"
        parts.append(f"{e.name}: {rendered} (derived from its own ledger).")
    return ToolResult(" ".join(parts), data={"balances": balances_by_employer})


def list_runs(ctx: ChatContext, employer: str | None = None, limit: int = 10) -> ToolResult:
    emp = resolve_employer(ctx, employer) if employer else None
    if employer and emp is None:
        return employer_not_resolved(ctx, employer)
    q = (select(PayrollRun).options(selectinload(PayrollRun.employer))
         .where(PayrollRun.organization_id == ctx.organization_id)
         .order_by(PayrollRun.created_at.desc()).limit(limit))
    if emp is not None:
        q = q.where(PayrollRun.employer_id == emp.id)
    rows = list(ctx.db.scalars(q))
    if not rows:
        return ToolResult("No payroll runs found for this organization yet.", data={"runs": []})
    lines = []
    for r in rows:
        extra = f", short by {fmt_usd(r.shortfall_usd)}" if r.shortfall_usd else ""
        lines.append(f"- {r.employer.name}: {r.status} (run {str(r.id)[:8]}, "
                     f"required {fmt_usd(r.required_usd) if r.required_usd is not None else 'not priced yet'}{extra})")
    data = {"runs": [{"run_id": str(r.id), "employer_name": r.employer.name, "status": r.status,
                      "required_usd": str(r.required_usd) if r.required_usd is not None else None,
                      "shortfall_usd": str(r.shortfall_usd) if r.shortfall_usd is not None else None}
                     for r in rows]}
    return ToolResult("Recent payroll runs:\n" + "\n".join(lines), data=data)


def explain_run(ctx: ChatContext, run_id: str | None = None, employer: str | None = None) -> ToolResult:
    emp = resolve_employer(ctx, employer) if employer else None
    if employer and emp is None:
        return employer_not_resolved(ctx, employer)
    run = resolve_run(ctx, run_id, emp)
    if run is None:
        return ToolResult("I cannot find that payroll run in this organization.",
                          data={"found": False})
    a = run.assessment or {}
    name = run.employer.name
    if not a:
        return ToolResult(f"Run {str(run.id)[:8]} for {name} is {run.status} and has not been priced yet.",
                          data={"run_id": str(run.id), "employer_name": name, "status": run.status,
                                "priced": False})
    per_currency = "; ".join(
        f"{r['currency']} {r['required']} (salaries {r['payroll']} + payout fees {r['fees']})"
        for r in a.get("by_currency", []))
    text = (
        f"{name}'s run {str(run.id)[:8]} is {run.status}. The math, from the stored assessment: "
        f"payroll needs {per_currency} — about {fmt_usd(a.get('payroll_usd', '0'))} — plus the "
        f"{fmt_usd(a.get('platform_fee_usd', '0'))} platform run fee, so {fmt_usd(a.get('required_usd', '0'))} "
        f"would leave the wallet, and a {fmt_usd(a.get('reserve_floor_usd', '0'))} reserve must stay untouched. "
        f"{name}'s own wallet holds {fmt_usd(a.get('available_usd', '0'))}."
    )
    if a.get("can_run"):
        text += " That covers the payroll, the fee and the reserve, so the run can proceed once a human approves it."
    else:
        text += (f" That leaves a shortfall of {fmt_usd(a.get('shortfall_usd', '0'))}, so the payroll is held. "
                 "Only this employer's own funds count — no other employer's money can cover it.")
    if run.explanation:
        text += f" Assessment note: {run.explanation}"
    approval = approval_payload(run) if run.pending_interrupt else None
    return ToolResult(text, approval=approval, data={
        "run_id": str(run.id), "employer_name": name, "status": run.status,
        "payroll_usd": str(a.get("payroll_usd", "0")),
        "required_usd": str(a.get("required_usd", "0")),
        "available_usd": str(a.get("available_usd", "0")),
        "shortfall_usd": str(a.get("shortfall_usd", "0")),
        "platform_fee_usd": str(a.get("platform_fee_usd", "0")),
        "reserve_floor_usd": str(a.get("reserve_floor_usd", "0")),
        "can_run": bool(a.get("can_run")),
    })


def get_run_timeline(ctx: ChatContext, run_id: str | None = None, employer: str | None = None) -> ToolResult:
    emp = resolve_employer(ctx, employer) if employer else None
    run = resolve_run(ctx, run_id, emp)
    if run is None:
        return ToolResult("I cannot find that payroll run in this organization.",
                          data={"found": False})
    if not run.events:
        return ToolResult(f"Run {str(run.id)[:8]} for {run.employer.name} has no recorded agent events yet.",
                          data={"run_id": str(run.id), "employer_name": run.employer.name,
                                "status": run.status, "events": []})
    lines = [f"- [{e.agent}] {e.message}" for e in run.events]
    return ToolResult(f"Timeline for {run.employer.name} run {str(run.id)[:8]} ({run.status}):\n" + "\n".join(lines),
                      data={"run_id": str(run.id), "employer_name": run.employer.name,
                            "status": run.status,
                            "events": [{"agent": e.agent, "message": e.message} for e in run.events]})


# ---------------------------------------------------------------------------
# Action tools (propose only — they can never approve or move money)
# ---------------------------------------------------------------------------

def start_payroll_run(ctx: ChatContext, employer: str | None = None) -> ToolResult:
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot start payroll runs.")
    emp = resolve_employer(ctx, employer)
    if emp is None:
        if employer:
            return employer_not_resolved(ctx, employer)
        return ToolResult("Which employer should I start a run for? Name one of this organization's employers.")
    if emp.status != "ACTIVE":
        return ToolResult(f"{emp.name} is not active, so a run cannot be started.")
    lines = [ctx.runtime.contractor_line(c, c.default_amount)
             for c in emp.contractors if c.is_active and c.default_amount is not None]
    if not lines:
        return ToolResult(f"{emp.name} has no contractors with a default amount on file, so there is nothing to run.")
    from ..db.models import Organization, User  # local import: models are cheap, keeps top imports light

    org = ctx.db.get(Organization, ctx.organization_id)
    user = ctx.db.get(User, ctx.user_id)
    try:
        run = ctx.runtime.start_run(ctx.db, org=org, user=user, employer=emp, pay_lines=lines,
                                    raw_input=None, source="chat")
    except RuntimeConflict as exc:
        ctx.db.rollback()
        return ToolResult(str(exc))
    ctx.db.expire_all()
    run = resolve_run(ctx, str(run.id))
    text = (f"Started a payroll run for {emp.name} (run {str(run.id)[:8]}). It is now {run.status}. "
            + (explain_run(ctx, str(run.id)).text if run.assessment else ""))
    approval = approval_payload(run) if run.pending_interrupt else None
    if approval:
        text += " The run is paused for a human decision — chat cannot approve it."
    return ToolResult(text, approval=approval, data={"run_id": str(run.id), "status": run.status})


def pay_held_lines(ctx: ChatContext, employer: str | None = None, run_id: str | None = None) -> ToolResult:
    """Start a continuation run that pays only a run's HELD lines. Like ``start_payroll_run`` it PAUSES
    for a human TAP-signed approval; chat cannot approve or move money."""
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot start payroll runs, so it cannot pay held lines.")
    emp = resolve_employer(ctx, employer)
    if emp is None:
        if employer:
            return employer_not_resolved(ctx, employer)
        return ToolResult("Which employer's held lines should I pay? Name one of this organization's employers.")
    if emp.status != "ACTIVE":
        return ToolResult(f"{emp.name} is not active, so its held lines cannot be paid.")
    parent = resolve_run(ctx, run_id) if (run_id or not employer) else None
    if parent is not None and parent.employer_id != emp.id:
        parent = None  # a run reference from another employer's page never redirects the named employer
    if parent is None:
        runs = list(ctx.db.scalars(
            select(PayrollRun).options(selectinload(PayrollRun.employer), selectinload(PayrollRun.pay_lines))
            .where(PayrollRun.organization_id == ctx.organization_id, PayrollRun.employer_id == emp.id,
                   PayrollRun.status.in_(CONTINUABLE)).order_by(PayrollRun.created_at.desc())))
        parent = next((r for r in runs if ctx.runtime.pay_held_blocker(ctx.db, r) is None),
                      runs[0] if runs else None)
        if parent is None:
            return ToolResult(f"{emp.name} has no held lines to pay: no run is partially paid or held for a "
                              "shortfall.", data={"employer_name": emp.name, "started": False})
    from ..db.models import Organization, User

    org = ctx.db.get(Organization, ctx.organization_id)
    user = ctx.db.get(User, ctx.user_id)
    try:
        run = ctx.runtime.start_continuation(ctx.db, org=org, user=user, run=parent)
    except (RuntimeConflict, HeldLineUnavailable) as exc:
        ctx.db.rollback()
        return ToolResult(f"I did not start it: {exc}", data={"employer_name": emp.name, "started": False})
    ctx.db.expire_all()
    run = resolve_run(ctx, str(run.id))
    text = (f"Started a continuation run for {emp.name} (run {str(run.id)[:8]}, continuing run "
            f"{str(parent.id)[:8]}). It pays only the held lines from {emp.name}'s own wallet; anyone already "
            f"paid is not paid again. It is now {run.status}. "
            + (explain_run(ctx, str(run.id)).text if run.assessment else ""))
    approval = approval_payload(run) if run.pending_interrupt else None
    if approval:
        text += " The run is paused for a human decision — chat cannot approve it."
    return ToolResult(text, approval=approval, data={"run_id": str(run.id), "status": run.status,
                                                     "parent_run_id": str(parent.id), "started": True})


def demo_fund_employer(ctx: ChatContext, employer: str | None = None, amount: str | None = None) -> ToolResult:
    if not ctx.demo_mode:
        return ToolResult("Funding an employer wallet is only available in demo mode.")
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot fund employers.")
    emp = resolve_employer(ctx, employer)
    if emp is None:
        if employer:
            return employer_not_resolved(ctx, employer)
        return ToolResult("Which employer should I fund? Name one of this organization's employers.")
    try:
        value = to_decimal(str(amount)) if amount else Decimal("0")
    except Exception:
        value = Decimal("0")
    if value <= 0:
        return ToolResult("Tell me a positive amount to fund, for example 'fund Borealis with USD 3000'.")
    ctx.runtime.fund(ctx.db, emp, value, reference=f"chat-{uuid.uuid4().hex[:12]}")
    ctx.db.commit()
    balances = ctx.runtime.balances(ctx.db, emp)
    rendered = ", ".join(f"{ccy} {amt}" for ccy, amt in balances.items()) or "no funds"
    return ToolResult(f"Funded {emp.name}'s own wallet with {fmt_usd(value)} (demo mode). "
                      f"Its balance is now {rendered}. This does not start or approve any payroll run.",
                      data={"employer_name": emp.name, "funded_usd": str(value),
                            "balances": dict(balances)})


def request_approval(ctx: ChatContext, run_id: str | None = None, employer: str | None = None) -> ToolResult:
    """Surface a run's pending human decision. This NEVER approves anything."""
    emp = resolve_employer(ctx, employer) if employer else None
    run = resolve_run(ctx, run_id, emp)
    if run is None:
        return ToolResult("I cannot find that payroll run in this organization.")
    if not run.pending_interrupt:
        return ToolResult(f"Run {str(run.id)[:8]} for {run.employer.name} is {run.status} and is not "
                          "waiting for a human decision.")
    payload = approval_payload(run)
    return ToolResult(
        f"Run {str(run.id)[:8]} for {run.employer.name} is waiting for a human decision. "
        f"{payload['summary']} I have surfaced it for approval — the decision itself must be made "
        "and TAP-signed on the run, never in chat.",
        approval=payload, data={"run_id": str(run.id), "status": run.status})


# ---------------------------------------------------------------------------
# Silo Benefits tools (read + propose only — deliberately NO approve/buy)
# ---------------------------------------------------------------------------

def _fmt_money(currency: str, value: object) -> str:
    return f"{currency} {to_decimal(str(value)):,.2f}"


def _vic_mode(ctx: ChatContext) -> str:
    """The Visa VIC mode from the runtime (``runtime.vic.mode``).

    The runtime exposes ``vic`` with ``.mode`` / ``.credential_ref``; older
    runtimes may not have it yet, in which case purchases are SIMULATED.
    """
    vic = getattr(ctx.runtime, "vic", None)
    mode = getattr(vic, "mode", None) if vic is not None else None
    return str(mode).strip().upper() if mode else "SIMULATED"


def _resolve_contractor(emp: Employer, ref: str | None):
    """Resolve a worker by name among one employer's *active* contractors.

    Returns ``(contractor, candidates)``: ``contractor`` is set only on an
    unambiguous match; ``candidates`` lists the partial matches when the
    reference is ambiguous (empty when nothing matched). Never guesses.
    """
    active = [c for c in emp.contractors if c.is_active]
    if not ref or not ref.strip():
        return None, []
    cleaned = ref.strip()
    lowered = cleaned.casefold()
    exact = [c for c in active if c.full_name.casefold() == lowered]
    if len(exact) == 1:
        return exact[0], exact
    if len(exact) > 1:
        return None, exact
    partial = [c for c in active
               if lowered in c.full_name.casefold()
               or cleaned.split()[0].casefold() == c.full_name.split()[0].casefold()
               or (len(cleaned.split()) == 1
                   and cleaned.casefold() == c.full_name.split()[-1].casefold())]
    if len(partial) == 1:
        return partial[0], partial
    return None, partial


def read_benefits(ctx: ChatContext, employer: str | None = None) -> ToolResult:
    """Read-only Silo Benefits view: budgets, payroll lock, recent requests."""
    try:
        from ..db.models import BenefitBudget, BenefitRequest
        from ..services import benefits as benefits_service
    except ImportError:
        return ToolResult(
            "Silo Benefits is not available in this deployment yet, so I cannot read "
            "benefit budgets or requests. No purchase has been made.",
            data={"benefits": [], "available": False})
    emp = resolve_employer(ctx, employer)
    if employer and emp is None:
        return ToolResult(f"I cannot find an employer named '{employer}' in {ctx.organization_name}.",
                          data={"employer": employer, "found": False})
    targets = [emp] if emp else employers(ctx)
    mode = _vic_mode(ctx)
    lines = [f"Benefit purchases run via Visa VIC and are currently in {mode} mode."]
    entries: list[dict] = []
    for e in targets:
        budgets = list(ctx.db.scalars(
            select(BenefitBudget).where(BenefitBudget.employer_id == e.id)
            .order_by(BenefitBudget.created_at)))
        try:
            held = bool(benefits_service.employer_payroll_held(ctx.db, e.id))
        except Exception:
            held = False
        requests = list(ctx.db.scalars(
            select(BenefitRequest).where(BenefitRequest.employer_id == e.id)
            .order_by(BenefitRequest.created_at.desc()).limit(5)))
        entry: dict = {
            "employer_id": str(e.id), "employer_name": e.name, "payroll_held": held,
            "budgets": [{"currency": b.currency, "monthly_cap": money_str(b.monthly_cap),
                         "spent_this_period": money_str(b.spent_this_period),
                         "remaining": money_str(to_decimal(str(b.monthly_cap))
                                                - to_decimal(str(b.spent_this_period))),
                         "per_tx_limit": money_str(b.per_tx_limit) if b.per_tx_limit is not None else None,
                         "period": b.period, "enabled": bool(b.enabled)} for b in budgets],
            "recent_requests": [{"contractor_name": r.contractor_name, "merchant": r.merchant,
                                 "item": r.item, "amount": money_str(r.amount),
                                 "currency": r.currency, "status": r.status} for r in requests],
        }
        entries.append(entry)
        if budgets:
            for b in budgets:
                remaining = to_decimal(str(b.monthly_cap)) - to_decimal(str(b.spent_this_period))
                per_tx = _fmt_money(b.currency, b.per_tx_limit) if b.per_tx_limit is not None else "US$200.00"
                period = f" for {b.period}" if b.period else ""
                lines.append(
                    f"{e.name}: benefits budget {b.currency} — monthly cap {_fmt_money(b.currency, b.monthly_cap)}, "
                    f"spent this period {_fmt_money(b.currency, b.spent_this_period)}, "
                    f"remaining {_fmt_money(b.currency, remaining)}{period} "
                    f"({'enabled' if b.enabled else 'disabled'}). "
                    f"Per-transaction limit: {per_tx} — while the published Airwallex test card "
                    "backs VIC, the per-transaction limit is US$200.00.")
        else:
            lines.append(f"{e.name}: no Silo Benefits budget is configured.")
        if held:
            lines.append(f"{e.name}: locked — payroll held; wages are funded first, so benefit "
                         "purchases for this employer are locked until payroll is funded.")
        if requests:
            lines.append(f"Most recent benefit requests for {e.name}:")
            for r in requests:
                lines.append(f"- {r.contractor_name}: {r.item} from {r.merchant} — "
                             f"{_fmt_money(r.currency, r.amount)} — {r.status}")
        else:
            lines.append(f"No benefit requests yet for {e.name}.")
    return ToolResult("\n".join(lines),
                      data={"benefits": entries, "vic_mode": mode, "available": True})


def propose_benefit_purchase(ctx: ChatContext, employer: str | None = None,
                             contractor: str | None = None, merchant: str | None = None,
                             item: str | None = None, amount: str | None = None,
                             currency: str | None = None) -> ToolResult:
    """Create a REQUESTED BenefitRequest. This NEVER approves or purchases."""
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot request benefit purchases.",
                          data={"created": False})
    try:
        from ..db.models import BenefitBudget
        from ..services import benefits as benefits_service
        from ..services.benefits import BenefitError
    except ImportError:
        return ToolResult(
            "Silo Benefits is not available in this deployment yet, so I could not create "
            "that request. Nothing has been purchased.",
            data={"created": False, "available": False})

    emp = resolve_employer(ctx, employer) if employer else resolve_employer(ctx, None)
    if employer and emp is None:
        return ToolResult(f"I cannot find an employer named '{employer}' in {ctx.organization_name}. "
                          "I have not created a benefit request. Nothing has been purchased.",
                          data={"created": False, "employer": employer, "found": False})
    worker = None
    if emp is None and contractor:
        # No employer named: infer it only when the worker belongs to
        # exactly one employer in this organization — otherwise refuse.
        matches: list[tuple[Employer, object]] = []
        for candidate_emp in employers(ctx):
            found, _cands = _resolve_contractor(candidate_emp, contractor)
            if found is not None:
                matches.append((candidate_emp, found))
        if len(matches) == 1:
            emp, worker = matches[0]
        elif len(matches) > 1:
            owners = ", ".join(f"{c.full_name} ({e.name})" for e, c in matches)
            return ToolResult(
                f"'{contractor}' works for more than one employer here ({owners}), so I will not "
                "guess which employer's benefit budget to use. Tell me the employer as well. "
                "I have not created a benefit request. Nothing has been purchased.",
                data={"created": False, "ambiguous_contractor": contractor})
        else:
            everyone = "; ".join(
                f"{e.name}: " + (", ".join(c.full_name for c in e.contractors if c.is_active) or "no active workers")
                for e in employers(ctx))
            return ToolResult(
                f"I cannot find a worker named '{contractor}' in {ctx.organization_name}. "
                f"Workers I can request benefits for are — {everyone}. "
                "I have not created a benefit request. Nothing has been purchased.",
                data={"created": False, "contractor": contractor, "found": False})
    if emp is None:
        names = ", ".join(e.name for e in employers(ctx)) or "none"
        return ToolResult(
            f"Which employer is this benefit for? Name one of this organization's employers ({names}) "
            "and the worker it is for. I have not created a benefit request. Nothing has been purchased.",
            data={"created": False})

    active_names = ", ".join(c.full_name for c in emp.contractors if c.is_active) or "no active workers"
    if worker is None:
        if not contractor:
            return ToolResult(
                f"Who is this benefit for? {emp.name}'s active workers are: {active_names}. "
                "I have not created a benefit request. Nothing has been purchased.",
                data={"created": False, "employer_name": emp.name})
        worker, candidates = _resolve_contractor(emp, contractor)
        if worker is None and candidates:
            matched = ", ".join(c.full_name for c in candidates)
            return ToolResult(
                f"'{contractor}' matches more than one of {emp.name}'s workers ({matched}), so I will "
                f"not guess. Please use the full name — {emp.name}'s active workers are: {active_names}. "
                "I have not created a benefit request. Nothing has been purchased.",
                data={"created": False, "employer_name": emp.name, "ambiguous_contractor": contractor})
        if worker is None:
            return ToolResult(
                f"I cannot find a worker named '{contractor}' among {emp.name}'s active workers: "
                f"{active_names}. I have not created a benefit request. Nothing has been purchased.",
                data={"created": False, "employer_name": emp.name, "contractor": contractor, "found": False})

    missing: list[str] = []
    if not merchant or not merchant.strip():
        missing.append("the merchant (where it would be bought)")
    if not item or not item.strip():
        missing.append("the item")
    value: Decimal | None = None
    if amount:
        try:
            value = to_decimal(str(amount))
        except Exception:
            value = None
    if value is None or value <= 0:
        missing.append("a positive amount")
    if missing:
        return ToolResult(
            f"To create that benefit request I still need: {', '.join(missing)}. For example: "
            f"'Request a benefit for {worker.full_name}: a 149 USD Python course on Coursera'. "
            "I have not created a benefit request. Nothing has been purchased.",
            data={"created": False, "employer_name": emp.name,
                  "contractor_name": worker.full_name, "missing": missing})

    chosen_currency = (currency or "").strip().upper()
    currency_defaulted = not chosen_currency
    if currency_defaulted:
        budget = ctx.db.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == emp.id)
                               .order_by(BenefitBudget.created_at))
        chosen_currency = budget.currency if budget is not None else "USD"
    try:
        request = benefits_service.create_request(
            ctx.db, organization_id=ctx.organization_id, employer=emp, contractor=worker,
            contractor_name=worker.full_name, merchant=merchant.strip(), item=item.strip(),
            amount=value, currency=chosen_currency,
            requested_by_id=ctx.user_id, requested_by_email=ctx.user_email)
    except BenefitError as exc:
        code = getattr(exc, "code", "benefit_error")
        detail = getattr(exc, "detail", None) or str(exc)
        return ToolResult(
            f"That benefit request could not be created ({code}): {detail} "
            "Nothing has been purchased.",
            data={"created": False, "employer_name": emp.name,
                  "contractor_name": worker.full_name, "error_code": code})
    request_id = str(request.id) if getattr(request, "id", None) is not None else None
    status = getattr(request, "status", "REQUESTED")
    ctx.db.commit()
    currency_note = (f"the employer's budget currency" if currency_defaulted else "as specified")
    text = (
        f"Created a Silo Benefits request for {worker.full_name} at {emp.name}: {item.strip()} "
        f"from {merchant.strip()} for {_fmt_money(chosen_currency, value)} "
        f"(currency {chosen_currency} — {currency_note}). The request is {status} and is now "
        "awaiting human approval in the Benefits screen — a human approver must approve it there. "
        "Nothing has been purchased, and I cannot approve or buy benefits: money moves only when "
        f"a human approves and buys in the Benefits screen, and any purchase then runs via Visa VIC "
        f"(currently in {_vic_mode(ctx)} mode)."
    )
    return ToolResult(text, data={
        "created": True, "request_id": request_id, "status": status,
        "employer_name": emp.name, "contractor_name": worker.full_name,
        "merchant": merchant.strip(), "item": item.strip(),
        "amount": money_str(value), "currency": chosen_currency,
        "currency_defaulted": currency_defaulted})


# ---------------------------------------------------------------------------
# VIC spend categories: Onboarding Kits + Travel & Accommodation
# (read + propose only — deliberately NO approve/purchase tool for either.
# Approvals and per-item purchases are human actions in the Onboarding /
# Travel screens — never chat tools.)
# ---------------------------------------------------------------------------

def _spend_target(ctx: ChatContext, employer: str | None, contractor: str | None,
                  *, not_created: str):
    """Resolve (employer, contractor) for a spend proposal, benefits-style.

    Returns ``(emp, worker, None)`` on success or ``(None, None, ToolResult)``
    with the refusal. Unknown or ambiguous workers/employers are refused —
    never guessed — and no row is created by the caller in that case.
    """
    emp = resolve_employer(ctx, employer) if employer else resolve_employer(ctx, None)
    if employer and emp is None:
        return None, None, ToolResult(
            f"I cannot find an employer named '{employer}' in {ctx.organization_name}. {not_created}",
            data={"created": False, "employer": employer, "found": False})
    worker = None
    if emp is None and contractor:
        matches: list[tuple[Employer, object]] = []
        for candidate_emp in employers(ctx):
            found, _cands = _resolve_contractor(candidate_emp, contractor)
            if found is not None:
                matches.append((candidate_emp, found))
        if len(matches) == 1:
            emp, worker = matches[0]
        elif len(matches) > 1:
            owners = ", ".join(f"{c.full_name} ({e.name})" for e, c in matches)
            return None, None, ToolResult(
                f"'{contractor}' works for more than one employer here ({owners}), so I will not "
                f"guess which employer's budget to use. Tell me the employer as well. {not_created}",
                data={"created": False, "ambiguous_contractor": contractor})
        else:
            everyone = "; ".join(
                f"{e.name}: " + (", ".join(c.full_name for c in e.contractors if c.is_active) or "no active workers")
                for e in employers(ctx))
            return None, None, ToolResult(
                f"I cannot find a worker named '{contractor}' in {ctx.organization_name}. "
                f"Workers I can help with are — {everyone}. {not_created}",
                data={"created": False, "contractor": contractor, "found": False})
    if emp is None:
        names = ", ".join(e.name for e in employers(ctx)) or "none"
        return None, None, ToolResult(
            f"Which employer is this for? Name one of this organization's employers ({names}). {not_created}",
            data={"created": False})
    active_names = ", ".join(c.full_name for c in emp.contractors if c.is_active) or "no active workers"
    if worker is None:
        if not contractor:
            return None, None, ToolResult(
                f"Who is this for? {emp.name}'s active workers are: {active_names}. {not_created}",
                data={"created": False, "employer_name": emp.name})
        worker, candidates = _resolve_contractor(emp, contractor)
        if worker is None and candidates:
            matched = ", ".join(c.full_name for c in candidates)
            return None, None, ToolResult(
                f"'{contractor}' matches more than one of {emp.name}'s workers ({matched}), so I will "
                f"not guess. Please use the full name — {emp.name}'s active workers are: {active_names}. "
                f"{not_created}",
                data={"created": False, "employer_name": emp.name, "ambiguous_contractor": contractor})
        if worker is None:
            return None, None, ToolResult(
                f"I cannot find a worker named '{contractor}' among {emp.name}'s active workers: "
                f"{active_names}. {not_created}",
                data={"created": False, "employer_name": emp.name, "contractor": contractor, "found": False})
    return emp, worker, None


def _totals_by_currency(rows) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        ccy = str(getattr(row, "currency", "USD") or "USD")
        totals[ccy] = totals.get(ccy, Decimal("0")) + to_decimal(str(getattr(row, "amount", "0")))
    return totals


_KIT_KIND_MERCHANTS = {"BACKGROUND_CHECK": "checkr.com", "GIFT_CARD": "amazon.com"}

_STANDARD_KIT: list[dict] = [
    {"kind": "BACKGROUND_CHECK", "label": "Background verification", "merchant": "checkr.com",
     "amount": Decimal("40"), "currency": "USD"},
    {"kind": "SAAS_SEAT", "label": "GitHub seat", "merchant": "github.com",
     "amount": Decimal("12"), "currency": "USD"},
    {"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "amazon.com",
     "amount": Decimal("50"), "currency": "USD"},
]

_KIT_AMOUNT_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _child_rows(ctx: ChatContext, parent, model, relationship: str,
                fk_names: tuple[str, ...]) -> list:
    """Child rows for a parent, whichever shape the model carries.

    Prefers the ORM relationship when the parent exposes one; otherwise
    queries by the first foreign-key column the child model actually has.
    Returns [] when neither exists — never guesses.
    """
    try:
        rel = getattr(parent, relationship, None)
    except Exception:
        rel = None
    if rel is not None:
        try:
            return list(rel)
        except TypeError:
            pass
    parent_id = getattr(parent, "id", None)
    if parent_id is None:
        return []
    for fk in fk_names:
        if hasattr(model, fk):
            return list(ctx.db.query(model).filter(getattr(model, fk) == parent_id).all())
    return []


def _owner_contractor_name(ctx: ChatContext, entity) -> str | None:
    """Display name of the contractor a kit/trip belongs to.

    The live Contractor row wins; the entity's own name snapshot is the
    fallback (a kit can outlive its contractor row — e.g. a seeded
    completed kit recorded against a name only). None when neither exists.
    """
    contractor_id = getattr(entity, "contractor_id", None)
    if contractor_id is not None:
        from ..db.models import Contractor  # local import, house style

        worker = ctx.db.get(Contractor, contractor_id)
        if worker is not None:
            return worker.full_name
    return getattr(entity, "contractor_name", None) or None


def _kit_kind(label: str) -> str:
    lowered = label.casefold()
    if "background" in lowered:
        return "BACKGROUND_CHECK"
    if "seat" in lowered or "software" in lowered:
        return "SAAS_SEAT"
    if "gift card" in lowered:
        return "GIFT_CARD"
    return "CUSTOM"


def _parse_kit_items(spec: str | None) -> list[dict]:
    """Parse "Background verification: $40; GitHub seat: $12" into item dicts.

    No/empty/unparseable spec -> the standard kit (checkr.com / github.com / amazon.com).
    """
    if not spec or not str(spec).strip():
        return [dict(item) for item in _STANDARD_KIT]
    items: list[dict] = []
    for segment in str(spec).split(";"):
        segment = segment.strip(" ,")
        if not segment:
            continue
        amount_match = None
        for amount_match in _KIT_AMOUNT_RE.finditer(segment):
            pass
        if amount_match is None:
            continue
        try:
            amount = to_decimal(amount_match.group(1).replace(",", ""))
        except Exception:
            continue
        if amount <= 0:
            continue
        label = segment[:amount_match.start()].strip(" :,-")
        label = " ".join(label.split()) or "Custom item"
        kind = _kit_kind(label)
        merchant = _KIT_KIND_MERCHANTS.get(kind) or (label.split()[0] if label.split() else "Merchant")
        items.append({"kind": kind, "label": label, "merchant": merchant,
                      "amount": amount, "currency": "USD"})
    return items or [dict(item) for item in _STANDARD_KIT]


def propose_onboarding_kit(ctx: ChatContext, employer: str | None = None,
                           contractor: str | None = None, items: str | None = None) -> ToolResult:
    """Create a DRAFT onboarding kit. This NEVER approves or purchases it."""
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot propose onboarding kits.",
                          data={"created": False})
    try:
        from ..services import onboarding as onboarding_service
    except ImportError:
        return ToolResult(
            "Onboarding kits are not available in this deployment yet, so I could not create "
            "that kit. Nothing has been purchased.",
            data={"created": False, "available": False})
    emp, worker, error = _spend_target(
        ctx, employer, contractor,
        not_created="I have not created an onboarding kit. Nothing has been purchased.")
    if error is not None:
        return error
    parsed_items = _parse_kit_items(items)
    try:
        kit = onboarding_service.create_kit(
            ctx.db, organization_id=ctx.organization_id, employer=emp, contractor=worker,
            items=parsed_items, created_by_id=ctx.user_id,
            created_by_email=ctx.user_email, source="CHAT")
    except Exception as exc:  # BE policy refusals surface as a refusal, never a purchase
        ctx.db.rollback()
        return ToolResult(
            f"That onboarding kit could not be created: {exc} "
            "I have not created an onboarding kit. Nothing has been purchased.",
            data={"created": False, "employer_name": emp.name, "contractor_name": worker.full_name})
    kit_id = str(kit.id) if getattr(kit, "id", None) is not None else None
    status = str(getattr(kit, "status", "DRAFT"))
    total = sum((item["amount"] for item in parsed_items), Decimal("0"))
    ctx.db.commit()
    item_lines = "; ".join(
        f"{item['label']} ({item['merchant']}) {_fmt_money(item['currency'], item['amount'])}"
        for item in parsed_items)
    text = (
        f"Created a {status} onboarding kit for {worker.full_name} at {emp.name}: "
        f"{len(parsed_items)} items totalling {_fmt_money('USD', total)} — {item_lines}. "
        "The kit is a draft: a human must approve the whole kit in the Onboarding screen, "
        "and purchases happen per item after approval. I cannot approve or buy onboarding "
        f"kits — purchases, when a human makes them, run via Visa VIC "
        f"(currently in {_vic_mode(ctx)} mode)."
    )
    return ToolResult(text, data={
        "created": True, "kit_id": kit_id, "status": status, "source": "CHAT",
        "employer_name": emp.name, "contractor_name": worker.full_name,
        "total": money_str(total), "currency": "USD",
        "items": [{"kind": i["kind"], "label": i["label"], "merchant": i["merchant"],
                   "amount": money_str(i["amount"]), "currency": i["currency"]} for i in parsed_items]})


def read_onboarding(ctx: ChatContext, employer: str | None = None) -> ToolResult:
    """Read-only onboarding view: budgets, kits and pending state.

    A kit's ``source`` / ``draft_reason`` are surfaced factually when the
    BE models carry them (display only — chat never drafts kits itself;
    drafting happens in workflows).
    """
    try:
        from ..db.models import KitItem, OnboardingBudget, OnboardingKit
    except ImportError:
        return ToolResult(
            "Onboarding kits are not available in this deployment yet, so I cannot read "
            "onboarding budgets or kits. Nothing has been purchased.",
            data={"onboarding": [], "available": False})
    emp = resolve_employer(ctx, employer)
    if employer and emp is None:
        return ToolResult(f"I cannot find an employer named '{employer}' in {ctx.organization_name}.",
                          data={"employer": employer, "found": False})
    targets = [emp] if emp else employers(ctx)
    mode = _vic_mode(ctx)
    lines = [f"Onboarding purchases run via Visa VIC and are currently in {mode} mode."]
    entries: list[dict] = []
    for e in targets:
        budgets = list(ctx.db.scalars(
            select(OnboardingBudget).where(OnboardingBudget.employer_id == e.id)
            .order_by(OnboardingBudget.created_at))) if hasattr(OnboardingBudget, "created_at") else \
            list(ctx.db.scalars(select(OnboardingBudget).where(OnboardingBudget.employer_id == e.id)))
        kit_query = select(OnboardingKit).where(OnboardingKit.employer_id == e.id)
        if hasattr(OnboardingKit, "organization_id"):
            kit_query = kit_query.where(OnboardingKit.organization_id == ctx.organization_id)
        if hasattr(OnboardingKit, "created_at"):
            kit_query = kit_query.order_by(OnboardingKit.created_at.desc())
        kits = list(ctx.db.scalars(kit_query.limit(10)))
        entry: dict = {
            "employer_id": str(e.id), "employer_name": e.name,
            "budgets": [{"currency": b.currency, "monthly_cap": money_str(b.monthly_cap),
                         "spent_this_period": money_str(b.spent_this_period),
                         "remaining": money_str(to_decimal(str(b.monthly_cap))
                                                - to_decimal(str(b.spent_this_period))),
                         "enabled": bool(b.enabled)} for b in budgets],
            "kits": [],
        }
        if budgets:
            for b in budgets:
                remaining = to_decimal(str(b.monthly_cap)) - to_decimal(str(b.spent_this_period))
                lines.append(
                    f"{e.name}: onboarding budget {b.currency} — monthly cap "
                    f"{_fmt_money(b.currency, b.monthly_cap)}, spent this period "
                    f"{_fmt_money(b.currency, b.spent_this_period)}, remaining "
                    f"{_fmt_money(b.currency, remaining)} ({'enabled' if b.enabled else 'disabled'}).")
        else:
            lines.append(f"{e.name}: no onboarding budget is configured.")
        if not kits:
            lines.append(f"No onboarding kits yet for {e.name}.")
        for kit in kits:
            items = _child_rows(ctx, kit, KitItem, "items", ("kit_id", "onboarding_kit_id"))
            totals = _totals_by_currency(items)
            total = totals.get("USD", sum(totals.values(), Decimal("0")))
            currency = "USD" if "USD" in totals else (next(iter(totals), "USD"))
            worker_name = _owner_contractor_name(ctx, kit) or "a contractor"
            status = str(getattr(kit, "status", ""))
            source = getattr(kit, "source", None)
            draft_reason = getattr(kit, "draft_reason", None)
            unpurchased = [i for i in items
                           if str(getattr(i, "status", "") or "").upper()
                           not in ("PURCHASED", "COMPLETED", "SUCCEEDED")]
            kit_entry = {
                "kit_id": str(kit.id), "status": status, "source": source,
                "draft_reason": draft_reason,
                "contractor_id": str(kit.contractor_id) if getattr(kit, "contractor_id", None) else None,
                "contractor_name": worker_name, "total": money_str(total), "currency": currency,
                "unpurchased_items": len(unpurchased),
                "items": [{"kind": getattr(i, "kind", None), "label": getattr(i, "label", None),
                           "merchant": getattr(i, "merchant", None),
                           "amount": money_str(i.amount) if getattr(i, "amount", None) is not None else None,
                           "currency": getattr(i, "currency", None),
                           "status": getattr(i, "status", None)} for i in items],
            }
            entry["kits"].append(kit_entry)
            rollup = "; ".join(
                f"{getattr(i, 'label', 'item')} ({getattr(i, 'merchant', '')}) "
                f"{_fmt_money(str(getattr(i, 'currency', 'USD')), getattr(i, 'amount', 0))}" for i in items)
            origin = {"TEMPLATE_AUTO": "drafted by a workflow",
                      "CHAT": "proposed in chat",
                      "MANUAL": "created manually"}.get(str(source or ""))
            origin_clause = f" ({origin})" if origin else ""
            reason_clause = f" Draft reason: {draft_reason}" if draft_reason else ""
            lines.append(
                f"{e.name}: onboarding kit for {worker_name} is {status}{origin_clause} — "
                f"{len(items)} items, total {_fmt_money(currency, total)}.{reason_clause}"
                + (f" Items: {rollup}." if rollup else ""))
            if status == "DRAFT":
                lines.append(f"Pending approval: the kit for {worker_name} at {e.name} "
                             f"({_fmt_money(currency, total)}) is a draft awaiting human approval "
                             "in the Onboarding screen.")
            elif status == "APPROVED" and unpurchased:
                lines.append(f"Pending purchase: the approved kit for {worker_name} at {e.name} "
                             f"still has {len(unpurchased)} unpurchased item(s) — purchases are "
                             "human actions in the Onboarding screen.")
        entries.append(entry)
    return ToolResult("\n".join(lines),
                      data={"onboarding": entries, "vic_mode": mode, "available": True})


def _trip_amount(value: str | None) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = to_decimal(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def propose_trip(ctx: ChatContext, employer: str | None = None,
                 contractor: str | None = None, destination: str | None = None,
                 transport: str | None = None, lodging: str | None = None,
                 per_diem: str | None = None) -> ToolResult:
    """Create a REQUESTED trip request. This NEVER approves or books it."""
    if ctx.role not in EDITOR_ROLES:
        return ToolResult(f"Your role ({ctx.role}) cannot propose trips.",
                          data={"created": False})
    try:
        from ..services import travel as travel_service
    except ImportError:
        return ToolResult(
            "Travel is not available in this deployment yet, so I could not create that "
            "trip request. Nothing has been booked or purchased.",
            data={"created": False, "available": False})
    emp, worker, error = _spend_target(
        ctx, employer, contractor,
        not_created="I have not created a trip request. Nothing has been booked or purchased.")
    if error is not None:
        return error
    if not destination or not destination.strip():
        return ToolResult(
            f"Where is this trip to? Tell me the destination for {worker.full_name}'s trip. "
            "I have not created a trip request. Nothing has been booked or purchased.",
            data={"created": False, "employer_name": emp.name, "contractor_name": worker.full_name})
    components: list[dict] = []
    for kind, label, merchant, raw in (
            ("TRANSPORT", "Transport", "Airline", transport),
            ("LODGING", "Lodging", "Hotel", lodging),
            ("PER_DIEM", "Per-diem", "Per-diem card", per_diem)):
        amount = _trip_amount(raw)
        if amount is not None:
            components.append({"kind": kind, "label": label, "merchant": merchant,
                               "amount": amount, "currency": "USD"})
    used_defaults = not components
    if used_defaults:
        components = [
            {"kind": "TRANSPORT", "label": "Airport transfer", "merchant": "Airline",
             "amount": Decimal("60"), "currency": "USD"},
            {"kind": "LODGING", "label": "Hotel night", "merchant": "Hotel",
             "amount": Decimal("180"), "currency": "USD"},
            {"kind": "PER_DIEM", "label": "Per-diem card", "merchant": "Per-diem card",
             "amount": Decimal("100"), "currency": "USD"},
        ]
    start = date.today() + timedelta(days=14)
    end = start + timedelta(days=2)
    try:
        trip = travel_service.create_trip(
            ctx.db, organization_id=ctx.organization_id, employer=emp, contractor=worker,
            destination=destination.strip(), start_date=start, end_date=end,
            purpose="Client work trip", components=components,
            created_by_id=ctx.user_id, created_by_email=ctx.user_email)
    except Exception as exc:
        ctx.db.rollback()
        return ToolResult(
            f"That trip request could not be created: {exc} "
            "I have not created a trip request. Nothing has been booked or purchased.",
            data={"created": False, "employer_name": emp.name, "contractor_name": worker.full_name})
    trip_id = str(trip.id) if getattr(trip, "id", None) is not None else None
    status = str(getattr(trip, "status", "REQUESTED"))
    total = sum((c["amount"] for c in components), Decimal("0"))
    ctx.db.commit()
    component_lines = "; ".join(
        f"{c['label']} ({c['merchant']}) {_fmt_money(c['currency'], c['amount'])}" for c in components)
    over_limit = [c for c in components if c["amount"] > Decimal("200")]
    text = (
        f"Created a {status} trip request for {worker.full_name} at {emp.name} to "
        f"{destination.strip()} ({start.isoformat()} to {end.isoformat()}, Client work trip): "
        f"{len(components)} components totalling {_fmt_money('USD', total)} — {component_lines}. "
        "A human must approve the trip in the Travel screen; travel budgets set category caps "
        "per component kind (transport, lodging, per-diem, other), and components are checked "
        "against those caps and the credential limit before any purchase. I cannot approve, "
        "book or buy travel. Note: any component above the active credential's per-transaction "
        "limit (USD 200 in SIMULATED mode) will be execution-blocked even when approved"
        + (f" — that includes: {', '.join(c['label'] for c in over_limit)}." if over_limit else ".")
        + f" Purchases, when a human makes them, run via Visa VIC (currently in {_vic_mode(ctx)} mode)."
    )
    if used_defaults:
        text += (" The component amounts are draft figures to edit in the Travel screen "
                 "before a human approves the trip.")
    return ToolResult(text, data={
        "created": True, "trip_id": trip_id, "status": status,
        "employer_name": emp.name, "contractor_name": worker.full_name,
        "destination": destination.strip(), "start_date": start.isoformat(),
        "end_date": end.isoformat(), "purpose": "Client work trip",
        "total": money_str(total), "currency": "USD", "used_defaults": used_defaults,
        "components": [{"kind": c["kind"], "label": c["label"], "merchant": c["merchant"],
                        "amount": money_str(c["amount"]), "currency": c["currency"]} for c in components]})


def read_travel(ctx: ChatContext, employer: str | None = None) -> ToolResult:
    """Read-only travel view: budgets (incl. category caps), trips, blocked components."""
    try:
        from ..db.models import TravelBudget, TripComponent, TripRequest
    except ImportError:
        return ToolResult(
            "Travel is not available in this deployment yet, so I cannot read travel "
            "budgets or trips. Nothing has been booked or purchased.",
            data={"travel": [], "available": False})
    emp = resolve_employer(ctx, employer)
    if employer and emp is None:
        return ToolResult(f"I cannot find an employer named '{employer}' in {ctx.organization_name}.",
                          data={"employer": employer, "found": False})
    targets = [emp] if emp else employers(ctx)
    mode = _vic_mode(ctx)
    lines = [f"Travel purchases run via Visa VIC and are currently in {mode} mode."]
    entries: list[dict] = []
    cap_fields = ("cap_transport", "cap_lodging", "cap_per_diem", "cap_other")
    for e in targets:
        budgets = list(ctx.db.scalars(select(TravelBudget).where(TravelBudget.employer_id == e.id)))
        trip_query = select(TripRequest).where(TripRequest.employer_id == e.id)
        if hasattr(TripRequest, "organization_id"):
            trip_query = trip_query.where(TripRequest.organization_id == ctx.organization_id)
        if hasattr(TripRequest, "created_at"):
            trip_query = trip_query.order_by(TripRequest.created_at.desc())
        trips = list(ctx.db.scalars(trip_query.limit(10)))
        budget_entries: list[dict] = []
        for b in budgets:
            entry_b: dict = {"currency": getattr(b, "currency", "USD"),
                             "enabled": bool(getattr(b, "enabled", False))}
            for attr in ("monthly_cap", "spent_this_period", "per_tx_limit", *cap_fields):
                value = getattr(b, attr, None)
                if value is not None:
                    entry_b[attr] = money_str(value) if not isinstance(value, bool) else value
            budget_entries.append(entry_b)
            caps = ", ".join(f"{attr.removeprefix('cap_')} {_fmt_money(entry_b['currency'], getattr(b, attr))}"
                             for attr in cap_fields if getattr(b, attr, None) is not None)
            base = ""
            if getattr(b, "monthly_cap", None) is not None:
                base = (f"monthly cap {_fmt_money(entry_b['currency'], b.monthly_cap)}, "
                        f"spent this period {_fmt_money(entry_b['currency'], getattr(b, 'spent_this_period', 0))}, ")
            lines.append(f"{e.name}: travel budget {entry_b['currency']} — {base}"
                         f"category caps: {caps or 'not set'} "
                         f"({'enabled' if entry_b['enabled'] else 'disabled'}).")
        if not budgets:
            lines.append(f"{e.name}: no travel budget is configured.")
        entry: dict = {"employer_id": str(e.id), "employer_name": e.name,
                       "budgets": budget_entries, "trips": []}
        if not trips:
            lines.append(f"No trips yet for {e.name}.")
        for trip in trips:
            components = _child_rows(ctx, trip, TripComponent, "components",
                                     ("trip_id", "trip_request_id", "request_id"))
            totals = _totals_by_currency(components)
            total = totals.get("USD", sum(totals.values(), Decimal("0")))
            currency = "USD" if "USD" in totals else (next(iter(totals), "USD"))
            worker_name = _owner_contractor_name(ctx, trip) or "a contractor"
            status = str(getattr(trip, "status", ""))
            destination = getattr(trip, "destination", None) or "an unspecified destination"
            trip_entry = {
                "trip_id": str(trip.id), "status": status, "destination": destination,
                "contractor_name": worker_name,
                "start_date": str(getattr(trip, "start_date", "")) or None,
                "end_date": str(getattr(trip, "end_date", "")) or None,
                "purpose": getattr(trip, "purpose", None),
                "total": money_str(total), "currency": currency,
                "components": [{"kind": getattr(c, "kind", None), "label": getattr(c, "label", None),
                                "merchant": getattr(c, "merchant", None),
                                "amount": money_str(c.amount) if getattr(c, "amount", None) is not None else None,
                                "currency": getattr(c, "currency", None),
                                "status": getattr(c, "status", None)} for c in components],
            }
            entry["trips"].append(trip_entry)
            rollup = "; ".join(
                f"{getattr(c, 'label', 'component')} ({getattr(c, 'merchant', '')}) "
                f"{_fmt_money(str(getattr(c, 'currency', 'USD')), getattr(c, 'amount', 0))} — "
                f"{getattr(c, 'status', 'PENDING')}" for c in components)
            lines.append(
                f"{e.name}: trip to {destination} for {worker_name} is {status} — "
                f"{len(components)} components, total {_fmt_money(currency, total)}."
                + (f" Components: {rollup}." if rollup else ""))
            for c in components:
                c_status = str(getattr(c, "status", "") or "").upper()
                if c_status in ("BLOCKED", "FAILED"):
                    reason = (getattr(c, "blocked_reason", None) or getattr(c, "failure_detail", None)
                              or getattr(c, "failure_code", None) or getattr(c, "reason", None)
                              or "no reason recorded")
                    lines.append(f"Blocked component on the {destination} trip: "
                                 f"{getattr(c, 'label', 'component')} — {c_status}: {reason}")
                    trip_entry.setdefault("blocked_components", []).append(
                        {"label": getattr(c, "label", None), "status": c_status, "reason": str(reason)})
            if status == "REQUESTED":
                lines.append(f"Pending approval: the trip to {destination} for {worker_name} "
                             f"({_fmt_money(currency, total)}) is awaiting human approval "
                             "in the Travel screen.")
        entries.append(entry)
    return ToolResult("\n".join(lines),
                      data={"travel": entries, "vic_mode": mode, "available": True})


def read_workflow_runs(ctx: ChatContext, employer: str | None = None, limit: int = 10) -> ToolResult:
    """Read-only: recent workflow (automation) runs for this organization.

    Workflows are managed in the Workflows screen, not in chat — this tool
    only reports run status. The Workflows module is built by another lane;
    its models are imported lazily here, and when they are unavailable in
    this deployment the tool says so instead of crashing.
    """
    try:
        from ..db.models import Workflow, WorkflowRun, WorkflowRunStep
    except ImportError:
        return ToolResult(
            "Workflows are not available in this deployment yet, so I cannot read "
            "workflow runs. Workflows are managed in the Workflows screen, not in chat.",
            data={"workflow_runs": [], "available": False})
    # Reading runs needs only the models (the workflows service, when
    # present, is what *executes* runs — chat never does).
    emp = resolve_employer(ctx, employer) if employer else None
    if employer and emp is None:
        return employer_not_resolved(ctx, employer)
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    query = select(WorkflowRun)
    if hasattr(WorkflowRun, "organization_id"):
        query = query.where(WorkflowRun.organization_id == ctx.organization_id)
    if emp is not None and hasattr(WorkflowRun, "employer_id"):
        query = query.where(WorkflowRun.employer_id == emp.id)
    if hasattr(WorkflowRun, "created_at"):
        query = query.order_by(WorkflowRun.created_at.desc())
    try:
        runs = list(ctx.db.scalars(query.limit(limit)))
    except Exception:
        return ToolResult(
            "Workflows are not available in this deployment yet, so I cannot read "
            "workflow runs. Workflows are managed in the Workflows screen, not in chat.",
            data={"workflow_runs": [], "available": False})
    if not runs:
        scope = f" for {emp.name}" if emp is not None else ""
        return ToolResult(f"No workflow runs yet{scope} in {ctx.organization_name}.",
                          data={"workflow_runs": [], "available": True})
    lines = []
    entries: list[dict] = []
    for run in runs:
        workflow_name = None
        rel = getattr(run, "workflow", None)
        if rel is not None and getattr(rel, "name", None):
            workflow_name = str(rel.name)
        workflow_id = getattr(run, "workflow_id", None)
        if workflow_name is None and workflow_id is not None:
            workflow_row = ctx.db.get(Workflow, workflow_id)
            workflow_name = str(workflow_row.name) if workflow_row is not None else None
        employer_name = None
        emp_rel = getattr(run, "employer", None)
        if emp_rel is not None and getattr(emp_rel, "name", None):
            employer_name = str(emp_rel.name)
        elif getattr(run, "employer_id", None) is not None:
            emp_row = ctx.db.get(Employer, run.employer_id)
            employer_name = emp_row.name if emp_row is not None else None
        steps = _child_rows(ctx, run, WorkflowRunStep, "steps",
                            ("workflow_run_id", "run_id"))
        status = str(getattr(run, "status", ""))
        entry = {"run_id": str(run.id), "workflow_name": workflow_name, "status": status,
                 "employer_name": employer_name,
                 "created_at": str(getattr(run, "created_at", "")) or None,
                 "steps": len(steps),
                 "step_statuses": [str(getattr(s, "status", "")) for s in steps]}
        entries.append(entry)
        label = workflow_name or "Workflow"
        for_whom = f" for {employer_name}" if employer_name else ""
        lines.append(f"- {label}{for_whom}: {status} (run {str(run.id)[:8]}, "
                     f"{len(steps)} step(s))")
    return ToolResult("Recent workflow runs:\n" + "\n".join(lines),
                      data={"workflow_runs": entries, "available": True})


def draft_workflow(ctx: ChatContext, description: str | None = None) -> ToolResult:
    """Draft a workflow graph from a plain-language request. Saved DISABLED, flagged as drafted by the agent, for a
    human to review, edit and enable on the canvas. Cannot enable, run, approve or delete anything."""
    from ..services import workflow_draft

    if not (description or "").strip():
        return ToolResult("Tell me what the workflow should do, for example 'monthly payroll for Acme on day 3'.",
                          data={"drafted": False})
    if ctx.role not in workflow_draft.DRAFT_ROLES:
        return ToolResult("Only an owner or admin can create workflows. Ask one of them to draft it.",
                          data={"drafted": False})
    model = DRAFT_MODEL_FACTORY(ctx) if DRAFT_MODEL_FACTORY is not None else _default_draft_model(ctx)
    if model is None:
        result = workflow_draft.draft_without_model(ctx.db, organization_id=ctx.organization_id,
                                                    user_id=ctx.user_id, description=description)
    else:
        result = workflow_draft.draft_with_model(ctx.db, model=model, organization_id=ctx.organization_id,
                                                 user_id=ctx.user_id, description=description)
    if not result.ok:
        return ToolResult(result.message, data={"drafted": False, "source": result.source})
    link = f"/dashboard/workflows/{result.workflow_id}"
    return ToolResult(f"{result.message} Review, edit and enable it here: [Open the workflow]({link}) "
                      "(I can't enable, run or approve workflows; that is your decision.)",
                      data={"drafted": True, "workflow_id": str(result.workflow_id), "link": link,
                            "enabled": False, "source": result.source, "attempts": result.attempts})


# Test seam: a callable (ctx) -> chat model or None. None means "use the configured provider".
DRAFT_MODEL_FACTORY = None


def _default_draft_model(ctx: ChatContext):
    from .llm import get_chat_model

    return get_chat_model(ctx.runtime.settings)


# Registry used by the LLM path: name -> (function, label, description).
TOOL_LABELS = {
    "list_employers": "Listing employers",
    "get_employer_balance": "Checking wallet balance",
    "list_runs": "Listing payroll runs",
    "explain_run": "Explaining a payroll run",
    "get_run_timeline": "Reading the run timeline",
    "start_payroll_run": "Starting a payroll run",
    "pay_held_lines": "Paying held lines in a continuation run (pauses for human approval)",
    "demo_fund_employer": "Funding an employer wallet (demo)",
    "request_approval": "Requesting a human approval",
    "read_benefits": "Reading Silo Benefits budgets and requests",
    "propose_benefit_purchase": "Proposing a benefit purchase (human approval required)",
    "read_onboarding": "Reading onboarding budgets and kits",
    "propose_onboarding_kit": "Proposing an onboarding kit (human approval required)",
    "read_travel": "Reading travel budgets and trips",
    "propose_trip": "Proposing a trip (human approval required)",
    "read_workflow_runs": "Reading workflow run status",
    "draft_workflow": "Drafting a workflow (saved disabled for a human to review)",
}

# Fuller descriptions for the tools whose limits the model must never blur.
# Used by the LLM path when present; every other tool keeps the label-based
# description in service.py.
TOOL_DESCRIPTIONS = {
    "read_benefits": (
        "Reading Silo Benefits budgets and requests. Read-only and scoped to the signed-in "
        "organization: per-employer benefit budget (monthly cap, spent, remaining, per-transaction "
        "limit), whether payroll is held for that employer, and the most recent benefit requests. "
        "Benefit purchases run via Visa VIC. This tool cannot buy, approve or change anything."),
    "propose_benefit_purchase": (
        "Proposing a benefit purchase (human approval required). Scoped to the signed-in "
        "organization. This creates a benefit request in REQUESTED status for a human approver "
        "in the Benefits screen — it does NOT approve the request and does NOT purchase or buy "
        "anything, and there is no tool that can. Never tell the user that you approved, bought "
        "or purchased a benefit; say the request was created and now awaits human approval."),
    "read_onboarding": (
        "Reading onboarding budgets and kits. Read-only and scoped to the signed-in organization: "
        "per-employer onboarding budget, kits with statuses and item rollups, each kit's source "
        "and draft reason when present, draft kits awaiting human approval, and "
        "approved kits with unpurchased items. Use it for onboarding status and for pending "
        "questions such as 'what's pending for a contractor's onboarding?'. Purchases run via Visa "
        "VIC. This tool cannot approve, buy or change anything."),
    "propose_onboarding_kit": (
        "Proposing an onboarding kit (human approval required). Scoped to the signed-in "
        "organization. This creates an onboarding kit in DRAFT status (source CHAT) for a human "
        "approver in the Onboarding screen — it does NOT approve the kit and does NOT purchase "
        "any item, and there is no tool that can. Items is a semicolon-separated spec like "
        "'Background verification: $40; GitHub seat: $12; Welcome gift card: $50'; omit it for "
        "the standard kit. Never tell the user that you approved or bought a kit item."),
    "read_travel": (
        "Reading travel budgets and trips. Read-only and scoped to the signed-in organization: "
        "per-employer travel budget including category caps (transport, lodging, per-diem, "
        "other), trips with component statuses, trips awaiting human approval, and blocked "
        "components with their reasons. Use it for travel status and for pending questions such "
        "as 'what's pending on the Berlin trip?'. Purchases run via Visa VIC. This tool cannot "
        "approve, book, buy or change anything."),
    "propose_trip": (
        "Proposing a trip (human approval required). Scoped to the signed-in organization. This "
        "creates a trip request in REQUESTED status for a human approver in the Travel screen — "
        "it does NOT approve, book or purchase anything, and there is no tool that can. Amounts "
        "are plain strings like '450' for transport, lodging and per-diem components; components "
        "without an amount are skipped, and with none given a default draft set is used. Any "
        "component above the active credential's per-transaction limit (USD 200 in SIMULATED "
        "mode) will be execution-blocked even when approved. Never tell the user that you "
        "approved or booked a trip."),
    "read_workflow_runs": (
        "Reading workflow run status. Read-only and scoped to the signed-in organization: "
        "recent workflow (automation) runs with their workflow name, employer, status and "
        "step counts. Workflows are managed in the Workflows screen, not in chat — this tool "
        "cannot create, edit, start or change any workflow or workflow run."),
    "draft_workflow": (
        "Drafting a workflow. Describe the automation in plain language (e.g. 'monthly payroll for Acme on "
        "day 3'). The graph is validated by code and saved DISABLED, flagged as drafted by Silo, for a human to "
        "review, edit and enable on the Workflows canvas; the reply links to it. This tool can never enable, run, "
        "approve or delete a workflow and cannot move money. Never say a drafted workflow is active."),
}

TOOL_FUNCTIONS = {
    "list_employers": list_employers,
    "get_employer_balance": get_employer_balance,
    "list_runs": list_runs,
    "explain_run": explain_run,
    "get_run_timeline": get_run_timeline,
    "start_payroll_run": start_payroll_run,
    "pay_held_lines": pay_held_lines,
    "demo_fund_employer": demo_fund_employer,
    "request_approval": request_approval,
    "read_benefits": read_benefits,
    "propose_benefit_purchase": propose_benefit_purchase,
    "read_onboarding": read_onboarding,
    "propose_onboarding_kit": propose_onboarding_kit,
    "read_travel": read_travel,
    "propose_trip": propose_trip,
    "read_workflow_runs": read_workflow_runs,
    "draft_workflow": draft_workflow,
}


from . import trace_tool  # noqa: E402,F401  (registers the read-only where_is_my_money tool)
