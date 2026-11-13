"""Silo Benefits — policy and purchase service (guide §6.5, invariant 13).

Deterministic code decides money here; no model is involved. The rules,
in order, for every purchase:

- A human approves the EXACT purchase snapshot (merchant, item, amount,
  currency, employer, expiry). The snapshot hash is recomputed at
  purchase time; any change invalidates the approval.
- The benefits budget is separate and can NEVER touch wage funds or
  reserves: purchases post only to ``benefits_expense`` /
  ``benefits_budget`` ledger accounts, never ``cash:``.
- If payroll for the employer is held, benefits are locked — wages are
  funded before benefits.
- Purchases are idempotent: a request that already has a SUCCEEDED
  purchase returns that purchase unchanged; retries use a new attempt
  number and a stable idempotency key per attempt.
- Mode is explicit on every purchase row (``SIMULATED`` or ``VIC``) and
  only masked credential references are stored.

Audit: AgentEvent is run-scoped (``run_id`` NOT NULL), so benefits cannot
use it. The append-only :class:`BenefitPurchase` rows plus
``decided_by`` / ``decided_at`` on the request ARE the audit trail.

This module deliberately does not import :mod:`app.services.runtime` at
module level (to avoid a cycle); the runtime is passed in as a parameter.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import BenefitBudget, BenefitPurchase, BenefitRequest, Contractor, Employer, PayrollRun, User, utcnow
from ..money import money_str, round_money
from ..partners.vic import (
    AWAITING_AUTH,
    TEST_CARD_TX_LIMIT_USD,
    CheckoutExecutor,
    HumanConfirmedCheckoutExecutor,
    VicError,
    VicUnavailable,
    merchant_url_for,
)

APPROVAL_TTL_DAYS = 7
HELD_RUN_STATUSES = ("HELD_SHORTFALL", "HELD_BY_HUMAN")


class BenefitError(Exception):
    """A benefits refusal with a stable machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def snapshot_hash(*, employer_id: uuid.UUID | str, merchant: str, item: str,
                  amount: Decimal, currency: str, expires_at: datetime) -> str:
    """Hash of the exact approved snapshot. Any change breaks the hash."""
    canonical = json.dumps({
        "employer_id": str(employer_id),
        "merchant": merchant,
        "item": item,
        "amount": money_str(amount),
        "currency": currency,
        "expires_at": expires_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def current_period() -> str:
    """Current budget period, ``YYYY-MM`` (UTC)."""
    return datetime.now(UTC).strftime("%Y-%m")


def employer_payroll_held(s: Session, employer_id: uuid.UUID) -> bool:
    """True when any payroll run for this employer is currently held."""
    return s.scalar(
        select(func.count()).select_from(PayrollRun)
        .where(PayrollRun.employer_id == employer_id, PayrollRun.status.in_(HELD_RUN_STATUSES))
    ) > 0


def budget_lock(s: Session, employer_id: uuid.UUID) -> tuple[bool, str | None]:
    if employer_payroll_held(s, employer_id):
        return True, "Payroll is held for this employer — wages are funded before benefits."
    return False, None


def create_request(s: Session, *, organization_id: uuid.UUID, employer: Employer,
                   contractor: Contractor | None, contractor_name: str | None,
                   merchant: str, item: str, amount: Decimal, currency: str,
                   requested_by_id: uuid.UUID | None, requested_by_email: str) -> BenefitRequest:
    name = contractor_name or (contractor.full_name if contractor is not None else None)
    if not name:
        raise BenefitError("CONTRACTOR_REQUIRED", "A contractor or contractor name is required.")
    req = BenefitRequest(
        organization_id=organization_id, employer_id=employer.id,
        contractor_id=contractor.id if contractor is not None else None,
        contractor_name=name, merchant=merchant, item=item,
        amount=round_money(amount), currency=currency, status="REQUESTED",
        requested_by_id=requested_by_id, requested_by_email=requested_by_email)
    s.add(req)
    s.flush()
    return req


def decide_request(s: Session, *, request: BenefitRequest, decision: str,
                   user: User, reason: str | None = None) -> BenefitRequest:
    if request.status != "REQUESTED":
        raise BenefitError("REQUEST_NOT_PENDING",
                           f"This request is {request.status} and can no longer be decided.")
    if decision not in ("approve", "deny"):
        raise BenefitError("INVALID_DECISION", "Decision must be 'approve' or 'deny'.")
    now = utcnow()
    request.decided_by_id = user.id
    request.decided_by_email = user.email
    request.decided_at = now
    if decision == "approve":
        budget = s.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == request.employer_id,
                                                      BenefitBudget.currency == request.currency))
        if budget is None or not budget.enabled:
            raise BenefitError("BUDGET_NOT_ENABLED",
                               f"This employer has no enabled {request.currency} benefits budget. An owner or admin "
                               "must set a monthly cap and enable the budget under Budgets by employer first.")
        expires = now + timedelta(days=APPROVAL_TTL_DAYS)
        request.status = "APPROVED"
        request.approval_expires_at = expires
        request.approval_hash = snapshot_hash(
            employer_id=request.employer_id, merchant=request.merchant, item=request.item,
            amount=request.amount, currency=request.currency, expires_at=expires)
        request.deny_reason = None
    elif decision == "deny":
        request.status = "DENIED"
        request.deny_reason = reason
        request.approval_hash = None
        request.approval_expires_at = None
    s.flush()
    return request


def execute_purchase(s: Session, runtime, *, request: BenefitRequest, user: User) -> BenefitPurchase:
    """Execute (or idempotently return) the purchase for an approved request.

    Pre-attempt refusals raise :class:`BenefitError`; a provider attempt
    that fails still returns its (FAILED) purchase row.
    """
    # 1. Idempotent success: never spend twice for one request.
    existing_success = s.scalar(
        select(BenefitPurchase).where(BenefitPurchase.request_id == request.id,
                                      BenefitPurchase.status == "SUCCEEDED")
        .order_by(BenefitPurchase.attempt_no))
    if existing_success is not None:
        return existing_success

    # 1b. Live two-phase: one mandate per purchase attempt. A retry while a
    # mandate is still awaiting the human returns that same pending purchase.
    existing_pending = s.scalar(
        select(BenefitPurchase).where(BenefitPurchase.request_id == request.id,
                                      BenefitPurchase.status == "PENDING")
        .order_by(BenefitPurchase.attempt_no))
    if existing_pending is not None:
        return existing_pending

    # 2. Only an approved request (or a failed attempt being retried) can buy.
    if request.status not in ("APPROVED", "FAILED"):
        raise BenefitError("REQUEST_NOT_APPROVED",
                           f"This request is {request.status}; only an approved request can be purchased.")

    # 3. The approved snapshot must still match, and must not have expired.
    if request.approval_expires_at is None or request.approval_hash is None:
        raise BenefitError("SNAPSHOT_MISMATCH", "This request has no valid approval snapshot.")
    expires = request.approval_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    recomputed = snapshot_hash(
        employer_id=request.employer_id, merchant=request.merchant, item=request.item,
        amount=request.amount, currency=request.currency, expires_at=expires)
    if recomputed != request.approval_hash:
        raise BenefitError("SNAPSHOT_MISMATCH",
                           "The merchant, item, amount or currency changed after approval. "
                           "A new approval is required.")
    if utcnow() > expires:
        raise BenefitError("APPROVAL_EXPIRED", "The approval for this purchase has expired. "
                                                "A new approval is required.")

    employer = s.get(Employer, request.employer_id)
    if employer is None:
        raise BenefitError("EMPLOYER_NOT_FOUND", "The employer for this request no longer exists.")

    # 4. A separate, enabled benefits budget must exist for this currency.
    budget = s.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == request.employer_id,
                                                  BenefitBudget.currency == request.currency))
    if budget is None or not budget.enabled:
        raise BenefitError("BUDGET_NOT_ENABLED",
                           f"No enabled {request.currency} benefits budget exists for this employer.")

    # 5. Wages first: a held payroll locks benefits.
    if employer_payroll_held(s, request.employer_id):
        raise BenefitError("PAYROLL_HELD", "Payroll for this employer is held; wages are funded before benefits.")

    # 6. Effective per-transaction limit (budget limit and test-card limit).
    limits: list[Decimal] = []
    if budget.per_tx_limit is not None:
        limits.append(Decimal(budget.per_tx_limit))
    if runtime.vic.test_card:
        limits.append(TEST_CARD_TX_LIMIT_USD)
    if limits and request.amount > min(limits):
        raise BenefitError("TX_LIMIT_EXCEEDED",
                           f"Amount {request.currency} {money_str(request.amount)} exceeds the "
                           f"per-transaction limit of {request.currency} {money_str(min(limits))}.")

    # 7. Period rollover, then the monthly cap.
    period = current_period()
    if budget.period != period:
        budget.spent_this_period = Decimal("0.00")
        budget.period = period
    if Decimal(budget.spent_this_period) + Decimal(request.amount) > Decimal(budget.monthly_cap):
        raise BenefitError("MONTHLY_CAP_EXCEEDED",
                           f"This purchase would exceed the {request.currency} monthly benefits cap of "
                           f"{money_str(budget.monthly_cap)} (already spent {money_str(budget.spent_this_period)}).")

    # 8. Attempt bookkeeping.
    attempt_count = s.scalar(
        select(func.count()).select_from(BenefitPurchase)
        .where(BenefitPurchase.request_id == request.id)) or 0
    attempt_no = int(attempt_count) + 1
    idempotency_key = f"benefit:{request.id}:{attempt_no}"

    # 9a. Live two-phase VIC: create an Airi mandate and wait for the human to
    # authorize it in Airi. Nothing is spent and no money moves in this phase.
    if getattr(runtime.vic, "two_phase", False):
        return _create_live_mandate(s, runtime, request=request, attempt_no=attempt_no,
                                    idempotency_key=idempotency_key)

    # 9. Provider attempt (simulated). A live-but-unavailable VIC fails closed.
    try:
        result = runtime.vic.purchase(merchant=request.merchant, amount=Decimal(request.amount),
                                      currency=request.currency, idempotency_key=idempotency_key)
    except VicUnavailable as exc:
        raise BenefitError("VIC_UNAVAILABLE", str(exc)) from None

    purchase = BenefitPurchase(
        organization_id=request.organization_id, employer_id=request.employer_id,
        request_id=request.id, attempt_no=attempt_no, idempotency_key=idempotency_key,
        mode=runtime.vic.mode, credential_ref=runtime.vic.credential_ref,
        provider_ref=result.provider_ref,
        status="SUCCEEDED" if result.ok else "FAILED",
        failure_code=result.failure_code, failure_detail=result.failure_detail,
        amount=Decimal(request.amount), currency=request.currency)
    s.add(purchase)
    s.flush()

    if result.ok:
        _finalize_success(s, runtime, request=request, purchase=purchase, employer=employer, budget=budget)
    else:
        # 11. Failed attempt: recorded, request can be retried.
        request.status = "FAILED"
    s.flush()
    return purchase


def _finalize_success(s: Session, runtime, *, request: BenefitRequest, purchase: BenefitPurchase,
                      employer: Employer, budget: BenefitBudget) -> None:
    """10. Success: spend from the benefits budget only. The journal touches
    benefits_* accounts exclusively — never cash — so reconciliation
    identities and golden payroll numbers cannot move."""
    purchase.status = "SUCCEEDED"
    request.status = "PURCHASED"
    budget.spent_this_period = Decimal(budget.spent_this_period) + Decimal(request.amount)
    ccy = request.currency
    journal = runtime.post_employer_journal(
        s, employer,
        [(f"{employer.ledger_key}:benefits_expense:{ccy}", Decimal(request.amount)),
         (f"{employer.ledger_key}:benefits_budget:{ccy}", -Decimal(request.amount))],
        memo=f"Benefit purchase — {request.item} · {request.merchant} (VIC {runtime.vic.mode})")
    purchase.journal_txn_id = journal.id


def _create_live_mandate(s: Session, runtime, *, request: BenefitRequest, attempt_no: int,
                         idempotency_key: str) -> BenefitPurchase:
    try:
        runtime.vic.ensure_ready()  # fail closed first: CLI present and signed in
    except VicUnavailable as exc:
        raise BenefitError("VIC_UNAVAILABLE", str(exc)) from None
    url = merchant_url_for(request.merchant)
    if url is None:
        raise BenefitError("MERCHANT_URL_REQUIRED",
                           "Live VIC needs the merchant as a website, e.g. 'coursera.org'. "
                           "Create a new request with the merchant's website.")
    try:
        info = runtime.vic.create_mandate(
            merchant=request.merchant, merchant_url=url, amount=Decimal(request.amount),
            currency=request.currency, item=request.item,
            description=f"PaySilo benefit: {request.item}"[:200])
    except VicUnavailable as exc:
        raise BenefitError("VIC_UNAVAILABLE", str(exc)) from None
    except VicError as exc:
        raise BenefitError(exc.code, exc.detail) from None
    purchase = BenefitPurchase(
        organization_id=request.organization_id, employer_id=request.employer_id,
        request_id=request.id, attempt_no=attempt_no, idempotency_key=idempotency_key,
        mode=runtime.vic.mode, credential_ref=info.payment_method_ref, provider_ref=None,
        status="PENDING", amount=Decimal(request.amount), currency=request.currency,
        provider_mandate_id=info.mandate_id, approval_url=info.approval_url,
        mandate_status=AWAITING_AUTH)
    s.add(purchase)
    s.flush()
    return purchase


def complete_purchase(s: Session, runtime, *, request: BenefitRequest, purchase: BenefitPurchase,
                      purchase_confirmed: bool, executor: CheckoutExecutor | None = None) -> BenefitPurchase:
    """Phase B: an explicit human click. Fetches the credential receipt and
    reports the mandate result. The model never calls this."""
    if purchase.status != "PENDING":
        return purchase  # idempotent: already settled
    if not purchase.provider_mandate_id:
        raise BenefitError("NO_MANDATE", "This purchase has no Airi mandate.")
    employer = s.get(Employer, request.employer_id)
    budget = s.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == request.employer_id,
                                                  BenefitBudget.currency == request.currency))
    if employer is None or budget is None or not budget.enabled:
        raise BenefitError("BUDGET_NOT_ENABLED",
                           f"No enabled {request.currency} benefits budget exists for this employer.")
    if employer_payroll_held(s, request.employer_id):
        raise BenefitError("PAYROLL_HELD", "Payroll for this employer is held; wages are funded before benefits.")
    period = current_period()
    if budget.period != period:
        budget.spent_this_period = Decimal("0.00")
        budget.period = period
    if Decimal(budget.spent_this_period) + Decimal(request.amount) > Decimal(budget.monthly_cap):
        raise BenefitError("MONTHLY_CAP_EXCEEDED", "This purchase would now exceed the monthly benefits cap.")

    executor = executor or HumanConfirmedCheckoutExecutor(purchase_confirmed)
    try:
        res = runtime.vic.complete(
            mandate_id=purchase.provider_mandate_id, merchant=request.merchant,
            amount=Decimal(request.amount), currency=request.currency, executor=executor)
    except VicUnavailable as exc:
        raise BenefitError("VIC_UNAVAILABLE", str(exc)) from None
    except VicError as exc:
        raise BenefitError(exc.code, exc.detail) from None

    if res.state == AWAITING_AUTH:
        purchase.mandate_status = AWAITING_AUTH
    elif res.ok:
        purchase.mandate_status = "COMPLETED"
        purchase.provider_ref = purchase.provider_mandate_id
        _finalize_success(s, runtime, request=request, purchase=purchase, employer=employer, budget=budget)
    else:
        purchase.status = "FAILED"
        purchase.mandate_status = res.state
        purchase.failure_code = res.failure_code
        purchase.failure_detail = res.failure_detail
        request.status = "FAILED"
    s.flush()
    return purchase


def cancel_purchase(s: Session, runtime, *, request: BenefitRequest, purchase: BenefitPurchase) -> BenefitPurchase:
    """Cancel a pending mandate (human action). The request can be retried."""
    if purchase.status != "PENDING" or not purchase.provider_mandate_id:
        raise BenefitError("NOT_PENDING", "Only a purchase awaiting authorization can be cancelled.")
    try:
        runtime.vic.cancel_mandate(purchase.provider_mandate_id)
    except VicUnavailable as exc:
        raise BenefitError("VIC_UNAVAILABLE", str(exc)) from None
    except VicError as exc:
        raise BenefitError(exc.code, exc.detail) from None
    purchase.status = "FAILED"
    purchase.mandate_status = "CANCELLED"
    purchase.failure_code = "MANDATE_CANCELLED"
    purchase.failure_detail = "The Airi mandate was cancelled before the purchase."
    request.status = "FAILED"
    s.flush()
    return purchase
