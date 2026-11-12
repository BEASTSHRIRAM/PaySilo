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

import uuid
from dataclasses import dataclass, field
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
}


from . import trace_tool  # noqa: E402,F401  (registers the read-only where_is_my_money tool)
