"""LangGraph state for one employer's payroll run.

Money crosses node boundaries as strings (exact Decimal text) so the
checkpointer never has to serialize Decimals. Nodes convert with
``to_decimal`` / ``money_str`` at the edges.

Status lifecycle: RECEIVED -> PRICED -> RESERVED -> APPROVED -> SENT ->
SETTLED, or -> HELD_SHORTFALL (escalated, awaiting the employer's own
funds) / HELD_BY_HUMAN (approval refused) / REFUSED (policy refusal).
"""

from __future__ import annotations

from typing import Optional, TypedDict


class PayLineDict(TypedDict):
    name: str
    amount: str  # Decimal text, e.g. "3400.00"
    currency: str


class PayrollRunState(TypedDict, total=False):
    run_id: str
    employer_key: str
    employer_name: str
    contact_name: str
    account_id: Optional[str]
    funding_usd: str
    pay_lines: list[PayLineDict]
    raw_input: Optional[str]
    # policy output (all Decimal text)
    assessment: Optional[dict]
    route: Optional[str]  # "approval" | "escalate" | "refused"
    decision: Optional[str]  # human decision from an interrupt
    status: str
    audit: list[str]
    fee_charged_usd: str
    metal_receipt: Optional[dict]
    explanation: Optional[str]
    error: Optional[str]
    balances: dict  # currency -> Decimal text, employer's own wallet at last check
