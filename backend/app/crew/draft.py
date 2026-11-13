"""Funding emails: deterministic templates, with an optional LLM polish that code double-checks.

The template is always the source of truth for every number, date and account detail. The model (when a
key is configured) may only re-word it, tool-less; its text is accepted ONLY if every currency amount,
date and account/routing identifier of the template appears verbatim and it introduces no new amount.
Otherwise the template is used unchanged.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from ..services.email_style import text_problems as _text_problems

logger = logging.getLogger(__name__)


def style_problems(text: str) -> list[str]:
    """House style breaches in reworded text: dashes, hyphens as punctuation, exclamation marks, emojis."""
    return _text_problems(text, full=False)

_AMOUNT = re.compile(r"\b[A-Z]{3} [\d,]+\.\d{2}\b")
_DATE = re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4}\b")

POLISH_SYSTEM = (
    "You edit a payroll funding email for clarity and warmth. Keep it short and professional. Never use dashes, hyphens, exclamation marks or emojis. "
    "Do NOT change, round, reorder or drop any amount, date, account number, routing number or name. "
    "Do not add new figures, promises or links. Return only the email body text."
)


def fmt_usd(value) -> str:
    return f"USD {Decimal(str(value)):,.2f}"


def fmt_date(value: str | date | None) -> str:
    if not value:
        return "the pay date"
    d = value if isinstance(value, date) else date.fromisoformat(str(value))
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


def _wire_block(wire: dict | None) -> str:
    if not wire or wire.get("status") != "READY":
        return "Account details: we will share them separately, because the bank is still issuing them."
    rows = [("Account name", wire.get("account_name")), ("Account number", wire.get("account_number")),
            ("Bank", wire.get("bank_name")), ("SWIFT/BIC", wire.get("swift_code"))]
    rows += [(r["label"], r["value"]) for r in wire.get("routing", []) if r.get("type") != "swift"]
    rows.append(("Payment reference", wire.get("reference")))
    return "Send funds to:\n" + "\n".join(f"  {k}: {v}" for k, v in rows if v)


def _lines_block(plan: dict) -> str:
    out = [f"  {ln['name']}: {ln['currency']} {Decimal(ln['amount']):,.2f}" for ln in plan.get("lines", [])]
    return "What it pays:\n" + "\n".join(out) if out else ""


def _breakdown(plan: dict) -> str:
    return "\n".join([
        f"  Contractor pay and payout fees: {fmt_usd(plan['payroll_usd'])}",
        f"  PaySilo run fee: {fmt_usd(plan['run_fee_usd'])}",
        f"  Safety reserve kept in the wallet: {fmt_usd(plan['reserve_floor_usd'])}",
        f"  Needed in the wallet: {fmt_usd(plan['required_in_wallet_usd'])}",
        f"  Already in the wallet: {fmt_usd(plan['available_usd'])}",
        f"  Still to send: {fmt_usd(plan['gap_usd'])}",
    ])


def draft_funding_email(kind: str, plan: dict, organization_name: str) -> tuple[str, str]:
    """(subject, body) for FUNDING_REMINDER, FUNDING_OVERDUE or FUNDING_STATEMENT. Pure and deterministic.

    House style (see ``services/email_style``): short, no dashes or hyphens as punctuation, greeting and closing.
    """
    from ..services.email_layout import clamp_subject

    greet = f"Hello {plan.get('contact_name')}," if plan.get("contact_name") else "Hello,"
    employer, rec = plan["employer_name"], fmt_usd(plan["recommended_transfer_usd"])
    fund_by, pay_date = fmt_date(plan.get("fund_by")), fmt_date(plan.get("pay_date"))
    sign = f"Thank you,\n{organization_name}"
    if kind == "FUNDING_STATEMENT":
        subject = f"Funding statement: {employer}, {pay_date}"
        intro = (f"This is the funding statement for the next {employer} payroll, paid on {pay_date}. "
                 f"Funds should be in the wallet by {fund_by}.")
        ask = (f"To pay everyone in full, please send {rec}." if Decimal(plan["gap_usd"]) > 0
               else "The wallet already covers this payroll. No transfer is needed.")
    elif kind == "FUNDING_OVERDUE":
        subject = f"Funding for {employer} was due {fund_by}"
        intro = (f"The funding for the {employer} payroll due on {pay_date} was expected by {fund_by} "
                 "and has not arrived in full.")
        ask = (f"Please send exactly {rec} now so that everyone is paid in full. "
               "If the money is already on its way, you can ignore this message.")
    else:
        subject = f"Funding needed for {employer} by {fund_by}"
        intro = (f"The next {employer} payroll is paid on {pay_date}. "
                 f"The wallet needs to be funded by {fund_by}.")
        ask = f"Please send exactly {rec} so that everyone is paid in full."
    body = "\n\n".join(x for x in (
        greet, intro, ask, "How this adds up:\n" + _breakdown(plan), _lines_block(plan), _wire_block(plan.get("wire")),
        "Transfers can take one to two business days to arrive. Reply to this email if anything looks wrong.",
        sign) if x)
    return clamp_subject(subject), body


def required_tokens(text: str, plan: dict | None = None) -> set[str]:
    """Everything that must survive polishing: amounts, dates and wire identifiers."""
    tokens = set(_AMOUNT.findall(text)) | set(_DATE.findall(text))
    wire = (plan or {}).get("wire") or {}
    if wire.get("status") == "READY":
        for v in (wire.get("account_number"), wire.get("swift_code"), wire.get("iban"),
                  *(r["value"] for r in wire.get("routing", []))):
            if v:
                tokens.add(str(v))
    return tokens


def accept_polish(template: str, polished: str, plan: dict | None = None) -> bool:
    """True only if the polished text keeps every required token and adds no new amount or date."""
    if not polished or not polished.strip():
        return False
    if any(tok not in polished for tok in required_tokens(template, plan)):
        return False
    if set(_AMOUNT.findall(polished)) - set(_AMOUNT.findall(template)):
        return False
    return not (set(_DATE.findall(polished)) - set(_DATE.findall(template)))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in content)
    return str(content or "")


def polish_body(body: str, plan: dict | None = None, *, llm=None, settings=None) -> tuple[str, bool]:
    """(text, polished?). ``llm`` is any object with ``invoke(messages)``; default: the chat LLM if keyed."""
    if llm is None and settings is not None:
        try:
            from ..chat.llm import get_chat_model
            llm = get_chat_model(settings)
        except Exception as exc:  # noqa: BLE001 - polish is optional
            logger.info("Email polish unavailable (%s).", type(exc).__name__)
            llm = None
    if llm is None:
        return body, False
    try:
        reply = llm.invoke([("system", POLISH_SYSTEM), ("human", body)])  # tool-less: no tools are bound
        candidate = _text(getattr(reply, "content", reply)).strip()
    except Exception as exc:  # noqa: BLE001
        logger.info("Email polish failed (%s); using the template.", type(exc).__name__)
        return body, False
    problems = style_problems(candidate)
    if problems:
        logger.info("Email polish rejected (house style: %s); using the template.", problems[0])
        return body, False
    if accept_polish(body, candidate, plan):
        return candidate, True
    logger.info("Email polish rejected (a number, date or account detail changed); using the template.")
    return body, False
