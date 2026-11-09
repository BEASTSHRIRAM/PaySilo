"""Supervisor: one batch entry point, one independent run per employer.

The supervisor is deterministic code, not a model (golden rule: the model
reads, the code decides). For each employer in the batch it invokes the
payroll graph as its own subgraph run:

- its own LangGraph thread (``thread_id`` = the run id), so checkpoints,
  interrupts and state are per-employer by construction;
- its own ``initial_state`` — no state dict, list or object is shared or
  mutated across employers;
- its own connected account / ledger namespace inside the services, which
  is the tenant silo the policy engine already enforces.

Outcomes are collected per employer as ``settled`` / ``held`` / ``failed``
(plus ``untouched`` for an employer with no payroll due, which is never
invoked at all). One employer's exception is caught, recorded as
``failed``, and never stops or contaminates the rest of the batch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.types import Command

from .graph import Services, build_graph, initial_state

logger = logging.getLogger(__name__)

# A decider answers a paused run's interrupt: (services, state, interrupt
# payload) -> resume value. In production the human decision arrives later
# through the API; in a batch the caller may supply a deterministic decider
# (the demo uses the TAP-signing demo operator, ``demo_resume``). With no
# decider, a paused run is reported as held/awaiting and left paused in
# its checkpoint.
Decider = Callable[[Services, dict, dict], dict]

_OUTCOME_BY_STATUS = {
    "SETTLED": "settled",
    "HELD_SHORTFALL": "held",
    "HELD_BY_HUMAN": "held",
    "APPROVAL_REJECTED": "held",
    "REFUSED": "failed",
    "FAILED": "failed",
}


def run_batch(services: Services, employers: list[dict[str, Any]], *,
              checkpointer=None, decider: Decider | None = None) -> dict[str, dict]:
    """Run one independent payroll subgraph per employer; collect outcomes.

    Each employer spec: ``{key, name, contact_name?, funding_usd?,
    pay_lines, run_id?}``. Returns ``{key: {outcome, status, run_id,
    employer, assessment, shortfall_usd, fee_charged_usd, balances,
    error}}``.
    """
    graph = build_graph(services, checkpointer=checkpointer)
    results: dict[str, dict] = {}

    for spec in employers:
        key = spec["key"]
        run_id = spec.get("run_id") or f"batch-{key}"
        if not spec.get("pay_lines"):
            # No payroll due: do not invoke the graph at all — the employer
            # (and its wallet) stays completely untouched.
            results[key] = {"outcome": "untouched", "status": "NO_PAYROLL_DUE", "run_id": None,
                            "employer": spec["name"], "assessment": None, "shortfall_usd": None,
                            "fee_charged_usd": "0.00", "balances": {}, "error": None}
            continue
        config = {"configurable": {"thread_id": run_id}}
        try:
            state = initial_state(
                run_id=run_id, employer_key=key, employer_name=spec["name"],
                contact_name=spec.get("contact_name", ""), funding_usd=spec.get("funding_usd", "0"),
                pay_lines=spec["pay_lines"], raw_input=spec.get("raw_input"),
                account_id=spec.get("account_id"), directory=spec.get("directory"))
            result = graph.invoke(state, config)
            while "__interrupt__" in result:
                if decider is None:
                    break  # leave it paused in its own checkpoint, awaiting a human
                payload = result["__interrupt__"][0].value
                current = graph.get_state(config).values
                result = graph.invoke(Command(resume=decider(services, current, payload)), config)
            final = graph.get_state(config).values
            status = final.get("status", "FAILED")
            outcome = _OUTCOME_BY_STATUS.get(status, "failed")
            if "__interrupt__" in result and decider is None:
                outcome = "held"  # paused, waiting for its human decision
            assessment = final.get("assessment") or {}
            results[key] = {
                "outcome": outcome, "status": status, "run_id": run_id,
                "employer": spec["name"], "assessment": final.get("assessment"),
                "shortfall_usd": assessment.get("shortfall_usd"),
                "fee_charged_usd": final.get("fee_charged_usd", "0.00"),
                "balances": final.get("balances", {}), "error": final.get("error"),
            }
        except Exception as exc:  # one employer's failure never affects another
            logger.exception("Batch run for employer %s failed", key)
            results[key] = {"outcome": "failed", "status": "FAILED", "run_id": run_id,
                            "employer": spec["name"], "assessment": None, "shortfall_usd": None,
                            "fee_charged_usd": "0.00", "balances": {}, "error": str(exc)}
    return results
