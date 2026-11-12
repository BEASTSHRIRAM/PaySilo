"""Shared two-phase VIC purchase mechanics for the spend categories (onboarding kits, travel).

Kits and trips buy several lines each, but every line goes through exactly the same adapter path as a
Silo Benefits purchase (:mod:`app.services.benefits`):

* Simulated mode (``VIC_MODE=simulated``): the adapter answers instantly and deterministically, so the
  purchase row is SUCCEEDED or FAILED at once.
* Live mode (``VIC_MODE=live``): ONE Airi mandate per purchase row. The row is PENDING with the human
  approval URL; nothing is spent until the human authorizes it in Airi and a person clicks Complete.
  Cancel marks the row FAILED so the line can be retried (a new mandate, a new attempt number).

Fail closed everywhere: an unavailable live CLI is a refusal, never a fallback to simulation. Only masked
credential references, mandate ids and approval URLs are stored: never card data.

Callers own their domain rules (approval snapshot, budget, wages-first lock, journals); this module only
owns "turn one approved line into one purchase row" and "complete / cancel one pending row".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..money import money_str
from ..partners.vic import (
    AWAITING_AUTH,
    TEST_CARD_TX_LIMIT_USD,
    CheckoutExecutor,
    HumanConfirmedCheckoutExecutor,
    VicError,
    VicUnavailable,
    merchant_url_for,
)
from .benefits import current_period


class SpendVicError(Exception):
    """A refusal from the shared VIC path with a stable machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class LineRef:
    """The purchasable line, independent of whether it is a kit item or a trip component."""

    id: uuid.UUID
    merchant: str
    amount: Decimal
    currency: str
    label: str


def tx_limit_hit(runtime, amount: Decimal, currency: str) -> bool:
    """True when the active credential is the capped test card and this line exceeds its limit."""
    return bool(getattr(runtime.vic, "test_card", False) and currency.upper() == "USD"
                and Decimal(amount) > TEST_CARD_TX_LIMIT_USD)


def check_budget_room(budget, amount: Decimal, currency: str, category: str) -> None:
    """Roll the period over if needed and refuse a line that would break the monthly cap."""
    period = current_period()
    if budget.period != period:
        budget.spent_this_period = Decimal("0.00")
        budget.period = period
    if Decimal(budget.spent_this_period) + Decimal(amount) > Decimal(budget.monthly_cap):
        raise SpendVicError(
            "MONTHLY_CAP_EXCEEDED",
            f"This purchase would exceed the {currency} monthly {category} cap of "
            f"{money_str(budget.monthly_cap)} (already spent {money_str(budget.spent_this_period)}).")


def next_attempt(s: Session, purchase_cls, line_fk, line_id: uuid.UUID) -> int:
    count = s.scalar(select(func.count()).select_from(purchase_cls).where(line_fk == line_id)) or 0
    return int(count) + 1


def existing_open(s: Session, purchase_cls, line_fk, line_id: uuid.UUID, status: str):
    return s.scalar(select(purchase_cls).where(line_fk == line_id, purchase_cls.status == status)
                    .order_by(purchase_cls.attempt_no))


def begin_purchase(s: Session, runtime, *, purchase_cls, common: dict, line: LineRef, attempt_no: int,
                   idempotency_key: str, category: str, user=None):
    """Create the purchase row for one line and return it.

    Returns a PENDING row (live mandate awaiting the human), a SUCCEEDED row (simulated) or a FAILED row
    (provider failure, including the honest TX_LIMIT_EXCEEDED). Raises :class:`SpendVicError` for pre-attempt
    refusals (VIC unavailable, merchant not a website in live mode, mandate could not be created).
    """
    audit = {"created_by_id": getattr(user, "id", None), "created_by_email": getattr(user, "email", None)}

    def make(**fields):
        row = purchase_cls(**common, attempt_no=attempt_no, idempotency_key=idempotency_key,
                           mode=runtime.vic.mode, amount=Decimal(line.amount), currency=line.currency,
                           **audit, **fields)
        s.add(row)
        s.flush()
        return row

    # Never faked, never split: a line above the credential's per-transaction limit is recorded as blocked.
    if tx_limit_hit(runtime, line.amount, line.currency):
        return make(credential_ref=runtime.vic.credential_ref, status="FAILED", failure_code="TX_LIMIT_EXCEEDED",
                    failure_detail=(f"The active credential allows USD {money_str(TEST_CARD_TX_LIMIT_USD)} per "
                                    f"transaction; {line.currency} {money_str(line.amount)} exceeds it."))

    if getattr(runtime.vic, "two_phase", False):
        try:
            runtime.vic.ensure_ready()  # fail closed first: CLI present and signed in
        except VicUnavailable as exc:
            raise SpendVicError("VIC_UNAVAILABLE", str(exc)) from None
        url = merchant_url_for(line.merchant)
        if url is None:
            raise SpendVicError("MERCHANT_URL_REQUIRED",
                                "Live VIC needs the merchant as a website, e.g. 'coursera.org'. "
                                "Edit the line with the merchant's website.")
        try:
            info = runtime.vic.create_mandate(
                merchant=line.merchant, merchant_url=url, amount=Decimal(line.amount), currency=line.currency,
                item=line.label, description=f"PaySilo {category}: {line.label}"[:200])
        except VicUnavailable as exc:
            raise SpendVicError("VIC_UNAVAILABLE", str(exc)) from None
        except VicError as exc:
            if exc.code == "TX_LIMIT_EXCEEDED":
                return make(credential_ref=runtime.vic.credential_ref, status="FAILED",
                            failure_code="TX_LIMIT_EXCEEDED", failure_detail=exc.detail)
            raise SpendVicError(exc.code, exc.detail) from None
        return make(credential_ref=info.payment_method_ref, status="PENDING", provider_ref=None,
                    provider_mandate_id=info.mandate_id, approval_url=info.approval_url,
                    mandate_status=AWAITING_AUTH)

    try:  # simulated mode: instant, deterministic (exactly what Silo Benefits does)
        result = runtime.vic.purchase(merchant=line.merchant, amount=Decimal(line.amount),
                                      currency=line.currency, idempotency_key=idempotency_key)
    except VicUnavailable as exc:
        raise SpendVicError("VIC_UNAVAILABLE", str(exc)) from None
    return make(credential_ref=runtime.vic.credential_ref, provider_ref=result.provider_ref,
                status="SUCCEEDED" if result.ok else "FAILED",
                failure_code=result.failure_code, failure_detail=result.failure_detail)


def complete_pending(runtime, purchase, line: LineRef, *, purchase_confirmed: bool,
                     executor: CheckoutExecutor | None = None) -> str:
    """Phase B for one PENDING row (an explicit human click). Returns COMPLETED | AWAITING | FAILED.

    The row's own fields are updated; the caller applies the domain consequences of COMPLETED / FAILED.
    """
    if not purchase.provider_mandate_id:
        raise SpendVicError("NO_MANDATE", "This purchase has no Airi mandate.")
    executor = executor or HumanConfirmedCheckoutExecutor(purchase_confirmed)
    try:
        res = runtime.vic.complete(mandate_id=purchase.provider_mandate_id, merchant=line.merchant,
                                   amount=Decimal(line.amount), currency=line.currency, executor=executor)
    except VicUnavailable as exc:
        raise SpendVicError("VIC_UNAVAILABLE", str(exc)) from None
    except VicError as exc:
        raise SpendVicError(exc.code, exc.detail) from None
    if res.state == AWAITING_AUTH:
        purchase.mandate_status = AWAITING_AUTH
        return "AWAITING"
    if res.ok:
        purchase.mandate_status = "COMPLETED"
        purchase.provider_ref = purchase.provider_mandate_id
        return "COMPLETED"
    purchase.status = "FAILED"
    purchase.mandate_status = res.state
    purchase.failure_code = res.failure_code
    purchase.failure_detail = res.failure_detail
    return "FAILED"


def cancel_pending(runtime, purchase) -> None:
    """Cancel one PENDING mandate (human action). The line can be retried with a new mandate."""
    if purchase.status != "PENDING" or not purchase.provider_mandate_id:
        raise SpendVicError("NOT_PENDING", "Only a purchase awaiting authorization can be cancelled.")
    try:
        runtime.vic.cancel_mandate(purchase.provider_mandate_id)
    except VicUnavailable as exc:
        raise SpendVicError("VIC_UNAVAILABLE", str(exc)) from None
    except VicError as exc:
        raise SpendVicError(exc.code, exc.detail) from None
    purchase.status = "FAILED"
    purchase.mandate_status = "CANCELLED"
    purchase.failure_code = "MANDATE_CANCELLED"
    purchase.failure_detail = "The Airi mandate was cancelled before the purchase."
