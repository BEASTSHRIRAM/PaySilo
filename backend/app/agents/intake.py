"""Intake Agent — the ONLY place an LLM is used in PaySilo.

The model *reads*: it turns a messy payroll sheet / email into structured
pay lines, and it explains decisions in plain language. It never decides:
pricing, affordability, fees and routing are deterministic code
(``app.policy``), and the graph supervisor routes on policy output only.

With ``ANTHROPIC_API_KEY`` set, Claude (via langchain-anthropic) does the
reading. Without a key, a deterministic fallback parser handles the
structured demo input — so demos and tests need no keys at all.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal

from pydantic import BaseModel, Field

from ..money import money_str, round_money, to_decimal
from ..policy import ContractorPayroll, PayrollAssessment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deterministic fallback parser
# ---------------------------------------------------------------------------

_PAY_RE = re.compile(
    r"pay\s+(.+?)\s+([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE
)
_FUND_RE = re.compile(r"fund\s+([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)
_EMPLOYER_RE = re.compile(r"employer\s*:\s*(.+)", re.IGNORECASE)


def parse_structured_input(raw: dict) -> tuple[str, Decimal, list[ContractorPayroll]]:
    """Parse the structured form: dict with employer/funding/pay_lines."""
    employer_name = str(raw.get("employer_name", "Unknown Employer"))
    funding = round_money(to_decimal(raw.get("funding_usd", "0")))
    lines = [
        ContractorPayroll(
            name=str(line["name"]),
            amount=round_money(to_decimal(line["amount"])),
            currency=str(line["currency"]).upper(),
            method=str(line.get("method") or "SWIFT").upper(),
        )
        for line in raw.get("pay_lines", [])
    ]
    return employer_name, funding, lines


def parse_text_input(text: str) -> tuple[str, Decimal, list[ContractorPayroll]]:
    """Deterministic parser for lines like:
    ``employer: Acme Manufacturing GmbH`` / ``fund USD 20000`` /
    ``pay Lena Fischer EUR 3400``.
    """
    employer_name = "Unknown Employer"
    funding = Decimal("0")
    lines: list[ContractorPayroll] = []
    for row in text.splitlines():
        row = row.strip()
        if not row:
            continue
        if m := _EMPLOYER_RE.match(row):
            employer_name = m.group(1).strip()
        elif m := _FUND_RE.match(row):
            funding = round_money(to_decimal(m.group(2).replace(",", "")))
        elif m := _PAY_RE.match(row):
            lines.append(
                ContractorPayroll(
                    name=m.group(1).strip(),
                    amount=round_money(to_decimal(m.group(3).replace(",", ""))),
                    currency=m.group(2).upper(),
                )
            )
    return employer_name, funding, lines


# ---------------------------------------------------------------------------
# Claude-powered intake (used only when ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------------

DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


class _PayLineOut(BaseModel):
    name: str = Field(description="Contractor's full name")
    amount: str = Field(description="Gross amount as a decimal string, e.g. '3400.00'")
    currency: str = Field(description="ISO 4217 currency code, e.g. EUR")
    method: str = Field(default="SWIFT", description="SWIFT or LOCAL")


class _PayrollInstructionOut(BaseModel):
    employer_name: str
    funding_usd: str = Field(default="0", description="USD the employer is funding, decimal string")
    pay_lines: list[_PayLineOut]


def _claude_parse(text: str, api_key: str) -> tuple[str, Decimal, list[ContractorPayroll]]:
    from langchain_anthropic import ChatAnthropic

    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_CLAUDE_MODEL)
    llm = ChatAnthropic(model=model, api_key=api_key, max_tokens=2048)  # no temperature: rejected by current Claude models
    reader = llm.with_structured_output(_PayrollInstructionOut)
    out: _PayrollInstructionOut = reader.invoke(
        "You are PaySilo's Intake Agent. Extract the payroll instruction below. Copy names and "
        "amounts exactly; never compute fees or totals; if something is missing, leave it out.\n\n"
        + text
    )
    return parse_structured_input(out.model_dump())


def parse_intake(raw: dict | str) -> tuple[str, Decimal, list[ContractorPayroll]]:
    """Entry point: Claude when a key is configured (text input only),
    deterministic fallback otherwise — and on any LLM error."""
    if isinstance(raw, dict):
        return parse_structured_input(raw)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return _claude_parse(raw, api_key)
        except Exception as exc:  # reading failed; the deterministic reader takes over
            logger.warning("Claude intake failed (%s); using deterministic parser", exc)
    return parse_text_input(raw)


# ---------------------------------------------------------------------------
# Plain-language explanations (deterministic templates)
# ---------------------------------------------------------------------------

def explain_assessment(employer_name: str, assessment: PayrollAssessment) -> str:
    a = assessment
    if a.payroll_currency == "MIXED":
        parts = ", ".join(f"{r.currency} {money_str(r.required)}" for r in a.by_currency)
        need = f"{parts} including payout fees (about USD {money_str(a.payroll_usd)})"
    else:
        need = (f"{a.payroll_currency} {money_str(a.total_required)} (salaries {money_str(a.total_payroll)} "
                f"+ payout fees {money_str(a.swift_fees)}), about USD {money_str(a.payroll_usd)}")
    fee = (f"no run fee (already collected on the original payroll)" if a.run_fee_waived
           else f"the USD {money_str(a.platform_fee_usd)} run fee")
    if a.can_run:
        return (
            f"{employer_name} can run this payroll: it needs {need}, plus {fee}, and must keep a USD {money_str(a.reserve_floor_usd)} "
            f"reserve. Its own wallet holds USD {money_str(a.available_usd)}. No other employer's money "
            "was considered."
        )
    return (
        f"{employer_name} cannot run this payroll yet: it needs {need}, plus {fee} and a USD {money_str(a.reserve_floor_usd)} reserve, "
        f"but its own wallet holds only USD {money_str(a.available_usd)} — short by USD "
        f"{money_str(a.shortfall_usd)}. The payroll is held and escalated to a human. It will not be "
        "covered from another employer's funds."
    )
