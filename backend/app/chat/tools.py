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
from ..money import to_decimal
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
}
