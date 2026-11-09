"""LangGraph state for one employer's payroll run.

Money crosses node boundaries as strings (exact Decimal text) so the
checkpointer never has to serialize Decimals. Nodes convert with
``to_decimal`` / ``money_str`` at the edges.

Status lifecycle: RECEIVED -> PRICED -> RESERVED -> APPROVED -> SENT ->
SETTLED (or PARTIALLY_PAID: a short employer's smallest lines were paid, the
rest stay HELD in ``held_lines``), or -> HELD_SHORTFALL (escalated, awaiting the employer's own
funds) / HELD_BY_HUMAN (human chose hold) / APPROVAL_REJECTED (approval
failed Visa TAP verification) / REFUSED (policy refusal) / FAILED (payout
step errored; safe to retry, every call is idempotent). After money moved:
SENT_FEE_FAILED (run fee not collected), SENT_UNRECONCILED (ledger != wallet),
ATTEST_PENDING (settled and reconciled, Metal attestation failed).
"""

from __future__ import annotations

from typing import Optional, TypedDict


class PayLineDict(TypedDict, total=False):
    name: str
    amount: str  # Decimal text, e.g. "3400.00"
    currency: str
    method: str  # "SWIFT" | "LOCAL"
    iban: str
    swift_code: str
    account_number: str
    routing_code: str
    country_code: str
    contractor_id: str


class PayrollRunState(TypedDict, total=False):
    run_id: str
    employer_key: str
    employer_name: str
    contact_name: str
    account_id: Optional[str]
    funding_usd: str
    pay_lines: list[PayLineDict]  # the lines THIS run pays (the affordable subset when partial)
    held_lines: list[PayLineDict]  # partial payout: lines escalated/held, never paid by this run
    raw_input: Optional[str]
    directory: list[PayLineDict]  # this employer's contractors (bank details), for intake matching
    # policy output (all Decimal text)
    assessment: Optional[dict]
    route: Optional[str]  # "approval" | "escalate" | "refused"
    decision: Optional[str]  # human decision from an interrupt
    decided_by: Optional[str]
    status: str
    audit: list[str]
    events: list[dict]  # {agent, agent_name, level, message} — structured audit for the UI
    signatures: list[dict]  # Visa TAP signatures over approvals and payout instructions
    fee_charged_usd: str
    completed_payouts: list[int]  # pay-line indices already paid AND posted to the ledger
    fx_done: list[str]  # currencies already converted (execute is resumable after FAILED)
    metal_receipt: Optional[dict]
    explanation: Optional[str]
    error: Optional[str]
    balances: dict  # currency -> Decimal text, employer's own wallet at last check
