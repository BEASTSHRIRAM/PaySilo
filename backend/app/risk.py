"""Deterministic payment-risk rules. Pure code: no model, no network, no clock except the ``now`` it is given.

A line that trips a rule is HELD (never paid by the run that found it) with a stable reason code:

``BANK_UNVERIFIED``
    The contractor's bank details are not VERIFIED (changed or newly submitted, waiting for a second
    person to confirm them by an independent callback).
``RECENT_BANK_CHANGE_LARGE_PAYMENT``
    The bank details changed within the last 7 days AND the line is large (more than 2x the contractor's
    previous paid amount in the same currency, or above USD 5,000 when there is no payment history),
    unless a SECOND person (not the one who made the change) verified them after a callback.

Lines carry their risk facts as plain strings (``bank_status``, ``bank_changed_at``,
``bank_second_verified``, ``prev_paid``) so they survive the graph checkpointer. A line without those
facts (not a directory contractor) is not judged here: the missing-bank-details refusal covers it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

BANK_UNVERIFIED = "BANK_UNVERIFIED"
RECENT_BANK_CHANGE_LARGE_PAYMENT = "RECENT_BANK_CHANGE_LARGE_PAYMENT"
RISK_CODES = (BANK_UNVERIFIED, RECENT_BANK_CHANGE_LARGE_PAYMENT)

RECENT_CHANGE_DAYS = 7
LARGE_MULTIPLE = Decimal("2")
NO_HISTORY_LIMIT_USD = Decimal("5000")

RISK_MESSAGES = {
    BANK_UNVERIFIED: "Bank details changed or not verified yet. A second person must verify them before this "
                     "payment can go out.",
    RECENT_BANK_CHANGE_LARGE_PAYMENT: "Bank details changed in the last 7 days and this payment is unusually large. "
                                      "A second person must verify the details by a callback first.",
}
RISK_FIELDS = ("bank_status", "bank_changed_at", "bank_second_verified", "prev_paid")


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def evaluate_line(line: dict, usd_rate: Decimal | None, now: datetime | None = None) -> str | None:
    """The risk reason code for one pay line, or ``None`` when it may be paid.

    ``usd_rate`` converts the line's currency to USD (only consulted when there is no payment history).
    Fail closed: a recently changed account whose size cannot be judged is treated as large.
    """
    status = line.get("bank_status")
    if status is None:
        return None
    if status != "VERIFIED":
        return BANK_UNVERIFIED
    changed = line.get("bank_changed_at")
    if not changed:
        return None
    now = now or datetime.now(UTC)
    try:
        changed_at = _aware(datetime.fromisoformat(str(changed)))
    except ValueError:
        return RECENT_BANK_CHANGE_LARGE_PAYMENT
    if now - changed_at > timedelta(days=RECENT_CHANGE_DAYS):
        return None
    if line.get("bank_second_verified") == "yes":
        return None
    amount = _dec(line.get("amount"))
    if amount is None:
        return RECENT_BANK_CHANGE_LARGE_PAYMENT
    previous = _dec(line.get("prev_paid")) if line.get("prev_paid") else None
    if previous is not None and previous > 0:
        large = amount > previous * LARGE_MULTIPLE
    elif usd_rate is None:
        large = True
    else:
        large = amount * usd_rate > NO_HISTORY_LIMIT_USD
    return RECENT_BANK_CHANGE_LARGE_PAYMENT if large else None


def split_lines(lines: list[dict], rates_fn, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """``(payable, held)``; each held line is a copy carrying ``hold_reason``. Order is preserved.

    ``rates_fn`` returns ``{currency: usd_rate}``; it is called at most once and only when a line needs it.
    """
    payable: list[dict] = []
    held: list[dict] = []
    rates: dict | None = None
    for line in lines:
        rate = None
        if line.get("bank_changed_at") and not line.get("prev_paid") and line.get("bank_status") == "VERIFIED":
            if rates is None:
                rates = rates_fn() or {}
            rate = _dec(rates.get(str(line.get("currency", "")).upper()))
        reason = evaluate_line(line, rate, now)
        if reason is None:
            payable.append(line)
        else:
            held.append({**line, "hold_reason": reason})
    return payable, held
