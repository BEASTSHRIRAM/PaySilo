"""Read-only Silo tool: "where is my money?" for a contractor's pay line.

Self-registers into the chat tool registry (see the last lines of tools.py).
Pure read: it only calls the deterministic ``payment_trace`` over rows of the
caller's own organization.
"""

from __future__ import annotations

from ..services import proof as proof_svc
from .tools import TOOL_FUNCTIONS, TOOL_LABELS, ChatContext, ToolResult, resolve_employer, resolve_run


def where_is_my_money(ctx: ChatContext, contractor: str | None = None, run_id: str | None = None,
                      employer: str | None = None) -> ToolResult:
    emp = resolve_employer(ctx, employer) if employer else None
    run = resolve_run(ctx, run_id, emp)
    if run is None:
        return ToolResult("I cannot find that payroll run in this organization.", data={"found": False})
    needle = (contractor or "").strip().lower()
    lines = [p for p in sorted(run.pay_lines, key=lambda p: p.position)
             if not needle or needle in p.beneficiary_name.lower()]
    if not lines:
        return ToolResult(f"No pay line matching '{contractor}' in {run.employer.name} run {str(run.id)[:8]}.",
                          data={"found": False})
    out, rows = [], []
    for p in lines[:6]:
        tr = proof_svc.line_facts(ctx.db, run, p)["trace"]
        out.append(f"- {p.beneficiary_name} ({p.currency} {p.amount}): {tr['state']}. {tr['diagnosis']} Next: {tr['next_step']}")
        rows.append({"contractor": p.beneficiary_name, "state": tr["state"], "failure_code": tr["failure_code"]})
    return ToolResult(f"Payment trace for {run.employer.name} run {str(run.id)[:8]}:\n" + "\n".join(out),
                      data={"run_id": str(run.id), "lines": rows})


TOOL_FUNCTIONS["where_is_my_money"] = where_is_my_money
TOOL_LABELS["where_is_my_money"] = "Tracing where a contractor's payment is"
