"""Onboarding Kit — policy and purchase service (mirrors services.benefits).

Deterministic code decides money. The rules mirror benefits exactly:

- A human approves the EXACT kit snapshot (employer, contractor and every
  item's kind/label/merchant/amount/currency, plus expiry). The snapshot
  hash is recomputed at purchase time; any change invalidates approval.
- The onboarding budget is separate and can NEVER touch wage funds:
  purchases post only to ``onboarding_expense`` / ``onboarding_budget``
  ledger accounts, never ``cash:``.
- If payroll for the employer is held, onboarding purchases are locked.
- Purchases go through the same two-phase VIC path as Silo Benefits
  (see :mod:`app.services.spend_common`): one purchase row (and, live,
  one Airi mandate) per item attempt; PENDING until a human completes it.
  Idempotent per item: an item with a SUCCEEDED purchase returns it
  unchanged; a PENDING one is returned instead of opening a second
  mandate; retries use a new attempt number and the stable key
  ``kit:{item_id}:{attempt_no}``. The kit is COMPLETED only when every
  item is PURCHASED.
- Provider failures are recorded as FAILED purchase rows. A
  TX_LIMIT_EXCEEDED failure leaves the item PENDING (it is blocked, not
  declined — see the tx_limit_blocked flag pattern in travel); other
  failures mark the item FAILED. Both states are retried by
  ``purchase_all``, which never re-buys a PURCHASED item.
- Mode is explicit on every purchase row and only masked credential
  references are stored.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    Contractor,
    Employer,
    KitItem,
    KitPurchase,
    OnboardingBudget,
    OnboardingKit,
    User,
    utcnow,
)
from ..money import money_str, round_money
from .benefits import APPROVAL_TTL_DAYS, employer_payroll_held
from .spend_common import (
    LineRef,
    SpendVicError,
    begin_purchase,
    cancel_pending,
    check_budget_room,
    complete_pending,
    existing_open,
    next_attempt,
)

__all__ = [
    "OnboardingError", "snapshot_hash", "create_kit", "decide_kit",
    "purchase_item", "purchase_all", "complete_item", "cancel_item", "draft_kit_from_template",
    "get_kit", "kit_email_context", "list_kits", "list_purchases", "budget_lock",
]

KIT_LEVEL_CODES = {
    "KIT_NOT_APPROVED", "SNAPSHOT_MISMATCH", "APPROVAL_EXPIRED",
    "PAYROLL_HELD", "BUDGET_NOT_ENABLED", "VIC_UNAVAILABLE",
}


class OnboardingError(Exception):
    """An onboarding refusal with a stable machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _item_dicts(items) -> list[dict]:
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append({"kind": it["kind"], "label": it["label"], "merchant": it["merchant"],
                        "amount": money_str(it["amount"]), "currency": it["currency"]})
        else:
            out.append({"kind": it.kind, "label": it.label, "merchant": it.merchant,
                        "amount": money_str(it.amount), "currency": it.currency})
    return out


def snapshot_hash(*, employer_id: uuid.UUID | str, contractor_id: uuid.UUID | str | None,
                  contractor_name: str, items, expires_at: datetime) -> str:
    """Hash of the exact approved snapshot. Any change breaks the hash."""
    canonical = json.dumps({
        "employer_id": str(employer_id),
        "contractor_id": str(contractor_id) if contractor_id else None,
        "contractor_name": contractor_name,
        "items": _item_dicts(items),
        "expires_at": expires_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def budget_lock(s: Session, employer_id: uuid.UUID) -> tuple[bool, str | None]:
    if employer_payroll_held(s, employer_id):
        return True, "Payroll is held for this employer — wages are funded before onboarding spend."
    return False, None


def create_kit(s: Session, *, organization_id: uuid.UUID, employer: Employer,
               contractor: Contractor | None, contractor_name: str | None = None,
               items: list[dict], created_by_id: uuid.UUID | None = None,
               created_by_email: str | None = None,
               source: str = "MANUAL", draft_reason: str | None = None) -> OnboardingKit:
    name = contractor_name or (contractor.full_name if contractor is not None else None)
    if not name:
        raise OnboardingError("CONTRACTOR_REQUIRED", "A contractor or contractor name is required.")
    if not items:
        raise OnboardingError("ITEMS_REQUIRED", "A kit needs at least one item.")
    kit = OnboardingKit(
        organization_id=organization_id, employer_id=employer.id,
        contractor_id=contractor.id if contractor is not None else None,
        contractor_name=name, status="DRAFT", source=source, draft_reason=draft_reason,
        created_by_id=created_by_id, created_by_email=created_by_email)
    s.add(kit)
    s.flush()
    for pos, spec in enumerate(items):
        s.add(KitItem(
            kit_id=kit.id, position=pos, kind=spec["kind"], label=spec["label"],
            merchant=spec["merchant"], amount=round_money(spec["amount"]),
            currency=spec.get("currency", "USD"), status="PENDING"))
    s.flush()
    s.refresh(kit)
    return kit


def _contractor_role(contractor: Contractor | None) -> str | None:
    """The Contractor model carries no role/title column today; read one
    honestly via getattr so a future column is picked up without a rewrite."""
    if contractor is None:
        return None
    for attr in ("role", "title", "job_title"):
        value = getattr(contractor, attr, None)
        if value:
            return str(value)
    return None


def draft_kit_from_template(s: Session, *, organization_id: uuid.UUID, employer: Employer,
                            contractor: Contractor, created_by=None) -> OnboardingKit:
    """Deterministic template draft for a newly created contractor.

    Idempotent: if a non-COMPLETED/DENIED kit already exists for this
    contractor, it is returned unchanged (contractor-creation retries
    must not duplicate kits).
    """
    existing = s.scalar(
        select(OnboardingKit)
        .where(OnboardingKit.organization_id == organization_id,
               OnboardingKit.contractor_id == contractor.id,
               OnboardingKit.status.notin_(("COMPLETED", "DENIED")))
        .order_by(OnboardingKit.created_at))
    if existing is not None:
        return existing
    role = (_contractor_role(contractor) or "").lower()
    items: list[dict] = [
        {"kind": "BACKGROUND_CHECK", "label": "Background verification", "merchant": "checkr.com",
         "amount": Decimal("40.00"), "currency": "USD"},
    ]
    if "engineer" in role or "developer" in role or "sde" in role:
        items.append({"kind": "SAAS_SEAT", "label": "GitHub seat", "merchant": "github.com",
                      "amount": Decimal("12.00"), "currency": "USD"})
        items.append({"kind": "SAAS_SEAT", "label": "Notion seat", "merchant": "notion.so",
                      "amount": Decimal("8.00"), "currency": "USD"})
    else:
        items.append({"kind": "SAAS_SEAT", "label": "Notion seat", "merchant": "notion.so",
                      "amount": Decimal("8.00"), "currency": "USD"})
    items.append({"kind": "GIFT_CARD", "label": "Welcome gift card", "merchant": "amazon.com",
                  "amount": Decimal("50.00"), "currency": "USD"})
    total = sum(Decimal(i["amount"]) for i in items)
    currency = items[0]["currency"]
    draft_reason = (f"Drafted by workflow from {employer.name}'s standard kit template — "
                    f"{len(items)} items, {currency} {money_str(total)}")
    created_by_id = getattr(created_by, "id", None) if created_by is not None else None
    created_by_email = getattr(created_by, "email", None) if created_by is not None else None
    return create_kit(
        s, organization_id=organization_id, employer=employer, contractor=contractor,
        items=items, created_by_id=created_by_id, created_by_email=created_by_email,
        source="WORKFLOW", draft_reason=draft_reason)


def decide_kit(s: Session, *, kit: OnboardingKit, decision: str,
               user: User, reason: str | None = None) -> OnboardingKit:
    if kit.status != "DRAFT":
        raise OnboardingError("KIT_NOT_PENDING",
                              f"This kit is {kit.status} and can no longer be decided.")
    if decision not in ("approve", "deny"):
        raise OnboardingError("INVALID_DECISION", "Decision must be 'approve' or 'deny'.")
    now = utcnow()
    kit.decided_by_id = user.id
    kit.decided_by_email = user.email
    kit.decided_at = now
    if decision == "approve":
        expires = now + timedelta(days=APPROVAL_TTL_DAYS)
        kit.status = "APPROVED"
        kit.approval_expires_at = expires
        kit.approval_hash = snapshot_hash(
            employer_id=kit.employer_id, contractor_id=kit.contractor_id,
            contractor_name=kit.contractor_name, items=list(kit.items), expires_at=expires)
        kit.deny_reason = None
    else:
        kit.status = "DENIED"
        kit.deny_reason = reason
        kit.approval_hash = None
        kit.approval_expires_at = None
    s.flush()
    return kit


def _rollup_kit_status(s: Session, kit: OnboardingKit) -> None:
    statuses = [i.status for i in kit.items]
    if statuses and all(st == "PURCHASED" for st in statuses):
        kit.status = "COMPLETED"
    elif any(st == "PURCHASED" for st in statuses):
        kit.status = "PARTIALLY_PURCHASED"


def _validate_kit(s: Session, kit: OnboardingKit) -> None:
    if kit.status not in ("APPROVED", "PARTIALLY_PURCHASED"):
        raise OnboardingError("KIT_NOT_APPROVED",
                              f"This kit is {kit.status}; only an approved kit can be purchased.")
    if kit.approval_expires_at is None or kit.approval_hash is None:
        raise OnboardingError("SNAPSHOT_MISMATCH", "This kit has no valid approval snapshot.")
    expires = kit.approval_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    recomputed = snapshot_hash(
        employer_id=kit.employer_id, contractor_id=kit.contractor_id,
        contractor_name=kit.contractor_name, items=list(kit.items), expires_at=expires)
    if recomputed != kit.approval_hash:
        raise OnboardingError("SNAPSHOT_MISMATCH",
                              "An item, the contractor or the employer changed after approval. "
                              "A new approval is required.")
    if utcnow() > expires:
        raise OnboardingError("APPROVAL_EXPIRED", "The approval for this kit has expired. "
                                                  "A new approval is required.")


def _ref(item: KitItem) -> LineRef:
    return LineRef(id=item.id, merchant=item.merchant, amount=Decimal(item.amount),
                   currency=item.currency, label=item.label)


def _budget_for(s: Session, kit: OnboardingKit, item: KitItem) -> OnboardingBudget:
    budget = s.scalar(select(OnboardingBudget).where(OnboardingBudget.employer_id == kit.employer_id,
                                                     OnboardingBudget.currency == item.currency))
    if budget is None or not budget.enabled:
        raise OnboardingError("BUDGET_NOT_ENABLED",
                              f"No enabled {item.currency} onboarding budget exists for this employer.")
    return budget


def _wages_first(s: Session, kit: OnboardingKit) -> None:
    if employer_payroll_held(s, kit.employer_id):
        raise OnboardingError("PAYROLL_HELD", "Payroll for this employer is held; wages are funded before onboarding spend.")


def _finalize_item(s: Session, runtime, *, kit: OnboardingKit, item: KitItem, purchase: KitPurchase,
                   employer: Employer, budget: OnboardingBudget) -> None:
    """Success: spend from the onboarding budget only (journal touches onboarding_* accounts, never cash)."""
    purchase.status = "SUCCEEDED"
    budget.spent_this_period = Decimal(budget.spent_this_period) + Decimal(item.amount)
    item.status = "PURCHASED"
    ccy = item.currency
    journal = runtime.post_employer_journal(
        s, employer,
        [(f"{employer.ledger_key}:onboarding_expense:{ccy}", Decimal(item.amount)),
         (f"{employer.ledger_key}:onboarding_budget:{ccy}", -Decimal(item.amount))],
        memo=f"Onboarding purchase — {item.label} · {item.merchant} (VIC {runtime.vic.mode})")
    purchase.journal_txn_id = journal.id
    _rollup_kit_status(s, kit)


def _item_after_failure(item: KitItem, purchase: KitPurchase) -> None:
    # TX-limit-blocked items stay PENDING (blocked, not declined); other failures mark the item FAILED.
    item.status = "PENDING" if purchase.failure_code == "TX_LIMIT_EXCEEDED" else "FAILED"


def purchase_item(s: Session, runtime, *, kit: OnboardingKit, item: KitItem,
                  user: User | None = None) -> KitPurchase:
    """Start (or idempotently return) the purchase for one kit item.

    One purchase row per attempt, one Airi mandate per row. In live mode the row is PENDING until the
    human authorizes the mandate and clicks Complete; simulated mode settles at once.
    """
    existing_success = existing_open(s, KitPurchase, KitPurchase.item_id, item.id, "SUCCEEDED")
    if existing_success is not None:
        return existing_success
    pending = existing_open(s, KitPurchase, KitPurchase.item_id, item.id, "PENDING")
    if pending is not None:
        return pending  # one mandate per purchase row: never open a second one
    if item.status == "PURCHASED":
        raise OnboardingError("SNAPSHOT_MISMATCH", "Item is PURCHASED but has no purchase record.")

    _validate_kit(s, kit)
    employer = s.get(Employer, kit.employer_id)
    if employer is None:
        raise OnboardingError("EMPLOYER_NOT_FOUND", "The employer for this kit no longer exists.")
    budget = _budget_for(s, kit, item)
    _wages_first(s, kit)
    try:
        check_budget_room(budget, item.amount, item.currency, "onboarding")
    except SpendVicError as exc:
        raise OnboardingError(exc.code, exc.detail) from None

    attempt_no = next_attempt(s, KitPurchase, KitPurchase.item_id, item.id)
    try:
        purchase = begin_purchase(
            s, runtime, purchase_cls=KitPurchase,
            common={"organization_id": kit.organization_id, "employer_id": kit.employer_id,
                    "kit_id": kit.id, "item_id": item.id},
            line=_ref(item), attempt_no=attempt_no, idempotency_key=f"kit:{item.id}:{attempt_no}",
            category="onboarding", user=user)
    except SpendVicError as exc:
        raise OnboardingError(exc.code, exc.detail) from None

    if purchase.status == "SUCCEEDED":
        _finalize_item(s, runtime, kit=kit, item=item, purchase=purchase, employer=employer, budget=budget)
    elif purchase.status == "FAILED":
        _item_after_failure(item, purchase)
    s.flush()
    return purchase


def complete_item(s: Session, runtime, *, kit: OnboardingKit, item: KitItem, purchase: KitPurchase,
                  purchase_confirmed: bool, executor=None) -> KitPurchase:
    """Phase B (explicit human click): fetch the credential receipt and report the mandate result."""
    if purchase.status != "PENDING":
        return purchase  # idempotent: already settled
    employer = s.get(Employer, kit.employer_id)
    budget = _budget_for(s, kit, item)
    if employer is None:
        raise OnboardingError("EMPLOYER_NOT_FOUND", "The employer for this kit no longer exists.")
    _wages_first(s, kit)
    try:
        check_budget_room(budget, item.amount, item.currency, "onboarding")
        outcome = complete_pending(runtime, purchase, _ref(item), purchase_confirmed=purchase_confirmed,
                                   executor=executor)
    except SpendVicError as exc:
        raise OnboardingError(exc.code, exc.detail) from None
    if outcome == "COMPLETED":
        _finalize_item(s, runtime, kit=kit, item=item, purchase=purchase, employer=employer, budget=budget)
    elif outcome == "FAILED":
        _item_after_failure(item, purchase)
    s.flush()
    return purchase


def cancel_item(s: Session, runtime, *, kit: OnboardingKit, item: KitItem, purchase: KitPurchase) -> KitPurchase:
    """Cancel a pending mandate (human action). The item can be retried."""
    try:
        cancel_pending(runtime, purchase)
    except SpendVicError as exc:
        raise OnboardingError(exc.code, exc.detail) from None
    item.status = "FAILED"
    s.flush()
    return purchase


def purchase_all(s: Session, runtime, *, kit: OnboardingKit,
                 user: User | None = None) -> dict:
    """Start each PENDING/FAILED item once. Never re-buys PURCHASED items nor opens a second mandate.

    Kit-level refusals (not approved, snapshot, expiry, held, budget, VIC unavailable) raise; per-item
    refusals (monthly cap) are collected into ``errors`` so later, smaller items can still be attempted.
    """
    _validate_kit(s, kit)
    purchases: list[KitPurchase] = []
    errors: list[dict] = []
    for item in list(kit.items):
        if item.status not in ("PENDING", "FAILED"):
            continue
        try:
            purchases.append(purchase_item(s, runtime, kit=kit, item=item, user=user))
        except OnboardingError as exc:
            if exc.code in KIT_LEVEL_CODES:
                raise
            errors.append({"item_id": str(item.id), "code": exc.code, "message": exc.detail})
    s.flush()
    return {"kit": kit, "purchases": purchases, "errors": errors}


def kit_email_context(s: Session, kit: OnboardingKit) -> dict:
    """Facts for the kit mail templates, from the kit's own recorded rows (never from a model)."""
    employer = s.get(Employer, kit.employer_id)
    latest: dict = {}
    for p in s.scalars(select(KitPurchase).where(KitPurchase.kit_id == kit.id, KitPurchase.status == "SUCCEEDED")):
        latest[p.item_id] = p
    items = [{"label": i.label, "kind": i.kind, "amount": money_str(i.amount), "currency": i.currency,
              "provider_ref": latest[i.id].provider_ref if i.id in latest else None,
              "mode": latest[i.id].mode if i.id in latest else None, "status": i.status}
             for i in kit.items]
    total = sum((Decimal(i.amount) for i in kit.items), Decimal("0"))
    currency = kit.items[0].currency if kit.items else "USD"
    return {"contractor_name": kit.contractor_name, "employer_name": employer.name if employer else "",
            "items": items, "lines": items, "total": money_str(total), "currency": currency,
            "entity_label": f"Onboarding kit for {kit.contractor_name}", "requested_for": kit.contractor_name,
            "kit_id": str(kit.id)}


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_kit(s: Session, organization_id: uuid.UUID, kit_id: uuid.UUID) -> OnboardingKit | None:
    return s.scalar(select(OnboardingKit).where(OnboardingKit.id == kit_id,
                                                OnboardingKit.organization_id == organization_id))


def list_kits(s: Session, organization_id: uuid.UUID,
              employer_id: uuid.UUID | None = None) -> list[OnboardingKit]:
    q = (select(OnboardingKit).where(OnboardingKit.organization_id == organization_id)
         .order_by(OnboardingKit.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(OnboardingKit.employer_id == employer_id)
    return list(s.scalars(q))


def list_purchases(s: Session, organization_id: uuid.UUID, *,
                   employer_id: uuid.UUID | None = None, kit_id: uuid.UUID | None = None,
                   item_id: uuid.UUID | None = None, status: str | None = None) -> list[KitPurchase]:
    q = (select(KitPurchase).where(KitPurchase.organization_id == organization_id)
         .order_by(KitPurchase.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(KitPurchase.employer_id == employer_id)
    if kit_id:
        q = q.where(KitPurchase.kit_id == kit_id)
    if item_id:
        q = q.where(KitPurchase.item_id == item_id)
    if status:
        q = q.where(KitPurchase.status == status)
    return list(s.scalars(q))
