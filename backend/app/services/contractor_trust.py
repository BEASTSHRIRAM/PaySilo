"""Contractor trust helpers: masking, tax-form status, bank-change detection, YTD / 1099 maths.

We are NOT tax software: only statuses and dates are stored, never tax ids or documents.

YTD derivation (persisted data only)
------------------------------------
For every PAID ``PayLine`` whose run was settled (or, failing that, created) in the current
calendar year, the USD equivalent is ``amount * rate`` where ``rate`` comes from the run's
stored ``assessment.by_currency`` entry for the line's currency: ``required_usd / required``
(the rate pricing used for that run). USD lines use rate 1. A line whose currency has no
stored rate is NOT guessed: it is counted in ``unpriced_lines`` and excluded from the USD total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Contractor, Employer, PayLine, PayrollRun
from ..money import round_money

TAX_EXPIRY_WARNING_DAYS = 60
BANK_FIELDS = ("iban", "swift_code", "account_number", "routing_code", "payout_method", "currency")

# The only payout options the self-serve portal offers (match what pricing supports).
PAYOUT_OPTIONS: list[dict] = [
    {"key": "EUR_SWIFT", "currency": "EUR", "method": "SWIFT", "label": "Euro - international transfer (SWIFT)",
     "fields": ["iban", "swift_code"]},
    {"key": "EUR_SEPA", "currency": "EUR", "method": "LOCAL", "label": "Euro - SEPA bank transfer",
     "fields": ["iban"]},
    {"key": "USD_ACH", "currency": "USD", "method": "LOCAL", "label": "US dollar - ACH (US bank account)",
     "fields": ["account_number", "routing_code"]},
    {"key": "GBP_FPS", "currency": "GBP", "method": "LOCAL", "label": "British pound - Faster Payments (UK bank account)",
     "fields": ["account_number", "routing_code"]},
]


def today_utc() -> date:
    return datetime.now(UTC).date()


def aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def mask(value: str | None) -> str:
    """Last 4 only. Short values are fully hidden."""
    if not value:
        return "none"
    return f"****{value[-4:]}" if len(value) > 4 else "****"


def effective_tax_form_status(c: Contractor, today: date | None = None) -> str:
    """RECEIVED with a past expiry date reads as EXPIRED (computed on read, never trusted stale)."""
    today = today or today_utc()
    if c.tax_form_status == "RECEIVED" and c.tax_form_expires_at is not None and c.tax_form_expires_at < today:
        return "EXPIRED"
    return c.tax_form_status


def bank_change_summary(old: dict, new: dict) -> str:
    """Masked, human summary of the bank fields that changed. Never contains full bank values."""
    labels = {"iban": "IBAN", "swift_code": "SWIFT/BIC", "account_number": "Account", "routing_code": "Routing/sort code"}
    parts: list[str] = []
    for key in BANK_FIELDS:
        if old.get(key) == new.get(key):
            continue
        if key in labels:
            parts.append(f"{labels[key]} {mask(old.get(key))} → {mask(new.get(key))}")
        else:  # payout method / currency are not bank values
            parts.append(f"{'Payout method' if key == 'payout_method' else 'Currency'} "
                         f"{old.get(key)} → {new.get(key)}")
    return "; ".join(parts)


def bank_snapshot(c: Contractor) -> dict:
    return {k: getattr(c, k) for k in BANK_FIELDS}


def validate_bank(runtime, emp: Employer, c: Contractor) -> dict:
    """Validate-only check through the Airwallex client/simulator. Creates nothing."""
    from ..airwallex.client import AirwallexError

    try:
        result = runtime.recorder.validate_beneficiary(
            on_behalf_of=emp.airwallex_account_id or "", name=c.full_name, currency=c.currency,
            country_code=c.country, method=c.payout_method, iban=c.iban or "",
            swift_code=c.swift_code or "", account_number=c.account_number or "",
            routing_code=c.routing_code or "")
        return {"valid": bool(result.get("valid")), "errors": list(result.get("errors", [])), "checked": True}
    except AirwallexError:
        return {"valid": False, "errors": [], "checked": False}


# ----------------------------------------------------------------------------- YTD / 1099
@dataclass
class YtdRow:
    usd: Decimal = Decimal("0.00")
    by_currency: dict[str, Decimal] = field(default_factory=dict)
    unpriced_lines: int = 0
    payments: int = 0


def _line_rate(run: PayrollRun, currency: str) -> Decimal | None:
    if currency == "USD":
        return Decimal("1")
    for row in (run.assessment or {}).get("by_currency", []) or []:
        if row.get("currency") != currency:
            continue
        try:
            required, required_usd = Decimal(str(row["required"])), Decimal(str(row["required_usd"]))
        except (KeyError, InvalidOperation):
            return None
        return required_usd / required if required > 0 else None
    return None


def ytd_paid(db: Session, organization_id, year: int) -> dict:
    """``{contractor_id: YtdRow}`` for PAID lines of runs in ``year`` (see module docstring)."""
    rows = db.execute(
        select(PayLine, PayrollRun).join(PayrollRun, PayLine.run_id == PayrollRun.id)
        .where(PayrollRun.organization_id == organization_id, PayLine.status == "PAID",
               PayLine.contractor_id.is_not(None))).all()
    out: dict = {}
    for line, run in rows:
        when = aware(run.settled_at or run.created_at)
        if when is None or when.year != year:
            continue
        row = out.setdefault(line.contractor_id, YtdRow())
        row.payments += 1
        row.by_currency[line.currency] = row.by_currency.get(line.currency, Decimal("0")) + line.amount
        rate = _line_rate(run, line.currency)
        if rate is None:
            row.unpriced_lines += 1
        else:
            row.usd += line.amount * rate
    for row in out.values():
        row.usd = round_money(row.usd)
    return out


def over_1099_threshold(c: Contractor, ytd_usd: Decimal, threshold: Decimal) -> bool:
    """US persons only; at-or-above the threshold counts (the rule is 'USD 2,000 or more')."""
    return c.tax_status == "US_PERSON" and ytd_usd >= threshold
