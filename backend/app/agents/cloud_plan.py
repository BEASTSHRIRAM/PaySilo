"""The pricing plan as a small LangGraph graph that runs anywhere, including Bedrock AgentCore.

The backend sends one payroll run's facts (pay lines, FX rates, that employer's own wallet) and gets
back the fees first assessment. It is the same ``assess_payroll`` policy code the main graph's Pricing
Agent uses, so the two results must be identical. The backend compares them before a run can reach
approval: an independent re computation in an isolated runtime, never a second source of truth.

Nothing here talks to Airwallex or a database, and nothing here can move money.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..money import to_decimal
from ..policy import ContractorPayroll, RefusalError, assess_payroll

PLAN_VERSION = 1


class PlanState(TypedDict, total=False):
    lines: list[dict]
    usd_value: dict[str, str]
    balances: dict[str, str]
    waive_run_fee: bool
    assessment: dict
    refusal: str


def plan_payload(lines: list[ContractorPayroll], usd_value: dict, balances: dict, waive_run_fee: bool) -> dict:
    """JSON safe facts for one run. Names are not sent: lines are identified by position."""
    return {
        "version": PLAN_VERSION,
        "lines": [{"amount": str(line.amount), "currency": line.currency, "method": line.method,
                   "priority": line.priority} for line in lines],
        "usd_value": {k: str(v) for k, v in usd_value.items()},
        "balances": {k: str(v) for k, v in balances.items()},
        "waive_run_fee": bool(waive_run_fee),
    }


def _price(state: PlanState) -> PlanState:
    from .graph import assessment_dict  # imported here: graph.py is the heavier module

    lines = [ContractorPayroll(name=f"line {i + 1}", amount=to_decimal(line["amount"]), currency=line["currency"],
                               method=line.get("method", "SWIFT"), priority=int(line.get("priority", 100)))
             for i, line in enumerate(state.get("lines") or [])]
    usd_value = {k: Decimal(str(v)) for k, v in (state.get("usd_value") or {}).items()}
    balances = {k: Decimal(str(v)) for k, v in (state.get("balances") or {}).items()}
    try:
        a = assess_payroll(lines, usd_value, balances, waive_run_fee=bool(state.get("waive_run_fee")))
    except RefusalError as exc:
        return {"refusal": str(exc)}
    return {"assessment": assessment_dict(a)}


def build_plan_graph():
    g = StateGraph(PlanState)
    g.add_node("price", _price)
    g.add_edge(START, "price")
    g.add_edge("price", END)
    return g.compile()


_GRAPH = None


def run_plan(payload: dict) -> dict:
    """Run the plan graph on one payload. Returns ``{"assessment": ...}`` or ``{"refusal": ...}``."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_plan_graph()
    out = _GRAPH.invoke({k: payload.get(k) for k in ("lines", "usd_value", "balances", "waive_run_fee")})
    result = {"version": PLAN_VERSION, "graph": "paysilo.plan", "nodes": ["price"]}
    if out.get("refusal"):
        result["refusal"] = out["refusal"]
    else:
        result["assessment"] = out["assessment"]
    return result
