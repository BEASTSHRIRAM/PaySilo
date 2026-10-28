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

import json
import os
import re
from decimal import Decimal

from ..money import money_str, round_money, to_decimal
from ..policy import ContractorPayroll, PayrollAssessment


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

def _claude_parse(text: str, api_key: str) -> tuple[str, Decimal, list[ContractorPayroll]]:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model="claude-sonnet-4-5", api_key=api_key, temperature=0)
    prompt = (
        "Extract a payroll instruction as JSON with keys employer_name (string), "
        "funding_usd (number), pay_lines (list of {name, amount, currency}). "
        "Return ONLY the JSON.\n\n" + text
    )
    reply = llm.invoke(prompt)
    content = reply.content if isinstance(reply.content, str) else str(reply.content)
    return parse_structured_input(json.loads(content))


def parse_intake(raw: dict | str) -> tuple[str, Decimal, list[ContractorPayroll]]:
    """Entry point: Claude when a key is configured (text input only),
    deterministic fallback otherwise — and on any LLM error."""
    if isinstance(raw, dict):
        return parse_structured_input(raw)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return _claude_parse(raw, api_key)
        except Exception:
            pass  # reading failed; the deterministic reader takes over
    return parse_text_input(raw)


# ---------------------------------------------------------------------------
# Plain-language explanations (template fallback; Claude optional)
# ---------------------------------------------------------------------------

def explain_assessment(employer_name: str, assessment: PayrollAssessment) -> str:
    if assessment.can_run:
        return (
            f"{employer_name} can run this payroll: it needs EUR {money_str(assessment.total_required)} "
            f"(salaries EUR {money_str(assessment.total_payroll)} + SWIFT fees EUR {money_str(assessment.swift_fees)}), "
            f"about USD {money_str(assessment.required_usd)}, and its own wallet holds "
            f"USD {money_str(assessment.available_usd)}. No other employer's money was considered."
        )
    return (
        f"{employer_name} cannot run this payroll yet: it needs about USD {money_str(assessment.required_usd)} "
        f"(EUR {money_str(assessment.total_required)} including SWIFT fees) but its own wallet holds only "
        f"USD {money_str(assessment.available_usd)} — a shortfall of USD {money_str(assessment.shortfall_usd)}. "
        "The payroll is held and escalated to a human. It will not be covered from another employer's funds."
    )
