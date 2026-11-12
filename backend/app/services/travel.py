"""Travel & Accommodation — policy and purchase service (mirrors onboarding).

Deterministic code decides money, exactly as in benefits/onboarding:

- A human approves the EXACT trip snapshot (employer, contractor,
  destination, dates, every component, and the caps-override flag).
- Per-trip category caps live on the employer's TravelBudget
  (transport 750 / lodging 600 / per-diem 300 / other 200 USD defaults).
  Approving a trip whose per-category total exceeds its cap requires an
  explicit ``override_caps`` decision, recorded as ``caps_overridden``.
- Purchases post only to ``travel_expense`` / ``travel_budget`` ledger
  accounts, are idempotent per component (key
  ``trip:{component_id}:{attempt_no}``), and are locked while payroll is
  held. Provider failures are FAILED purchase rows; a TX_LIMIT_EXCEEDED
  failure leaves the component PENDING (blocked, not declined).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import (
    Contractor,
    Employer,
    TravelBudget,
    TripComponent,
    TripPurchase,
    TripRequest,
    User,
    utcnow,
)
from ..money import money_str, round_money
from ..partners.vic import TEST_CARD_TX_LIMIT_USD, VicUnavailable
from .benefits import APPROVAL_TTL_DAYS, current_period, employer_payroll_held

__all__ = [
    "TravelError", "snapshot_hash", "create_trip", "decide_trip",
    "purchase_component", "purchase_all", "policy_summary",
    "category_cap", "category_totals", "tx_limit_for", "component_flags",
    "get_trip", "list_trips", "list_purchases", "budget_lock",
    "DEFAULT_CAPS", "CATEGORY_LABELS",
]

DEFAULT_CAPS: dict[str, Decimal] = {
    "TRANSPORT": Decimal("750.00"), "LODGING": Decimal("600.00"),
    "PER_DIEM": Decimal("300.00"), "OTHER": Decimal("200.00"),
}
CATEGORY_LABELS: dict[str, str] = {
    "TRANSPORT": "Transport", "LODGING": "Lodging",
    "PER_DIEM": "Per-diem", "OTHER": "Other",
}
_CAP_ATTRS: dict[str, str] = {
    "TRANSPORT": "cap_transport", "LODGING": "cap_lodging",
    "PER_DIEM": "cap_per_diem", "OTHER": "cap_other",
}
TRIP_LEVEL_CODES = {
    "TRIP_NOT_APPROVED", "SNAPSHOT_MISMATCH", "APPROVAL_EXPIRED",
    "PAYROLL_HELD", "BUDGET_NOT_ENABLED", "VIC_UNAVAILABLE",
}


class TravelError(Exception):
    """A travel refusal with a stable machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def category_cap(budget: TravelBudget | None, kind: str) -> Decimal:
    if budget is None:
        return DEFAULT_CAPS[kind]
    return Decimal(getattr(budget, _CAP_ATTRS[kind]))


def category_totals(trip: TripRequest) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for comp in trip.components:
        totals[comp.kind] = totals.get(comp.kind, Decimal("0.00")) + Decimal(comp.amount)
    return totals


def tx_limit_for(adapter) -> Decimal | None:
    """The active adapter's per-transaction limit, honestly obtained.

    The simulated (test-card) adapter's cap is TEST_CARD_TX_LIMIT_USD.
    A live Airi adapter reports a limit only when it is itself a test
    card; for a real credential the limit is unknown -> None (never
    reported as blocked).
    """
    if adapter is None:
        return TEST_CARD_TX_LIMIT_USD
    if getattr(adapter, "test_card", False):
        return TEST_CARD_TX_LIMIT_USD
    return None


def component_flags(trip: TripRequest, component: TripComponent,
                    budget: TravelBudget | None, adapter=None) -> dict:
    """Serializer flags for one component: over_cap / cap / tx_limit_blocked."""
    cap = category_cap(budget, component.kind)
    total = category_totals(trip).get(component.kind, Decimal("0.00"))
    limit = tx_limit_for(adapter)
    blocked = bool(
        limit is not None and component.currency == "USD"
        and Decimal(component.amount) > limit and component.status != "PURCHASED")
    return {"over_cap": bool(total > cap), "cap": money_str(cap), "tx_limit_blocked": blocked}


def _fmt_limit(limit: Decimal) -> str:
    return str(int(limit)) if limit == int(limit) else money_str(limit)


def policy_summary(trip: TripRequest, budget: TravelBudget | None, adapter=None) -> str:
    """Deterministic plain-English policy summary. No LLM.

    Built from the same cap/limit computations as the component flags.
    """
    totals = category_totals(trip)
    sentences: list[str] = []
    over: list[tuple[str, Decimal, Decimal]] = []
    for kind in ("TRANSPORT", "LODGING", "PER_DIEM", "OTHER"):
        if kind not in totals:
            continue
        cap = category_cap(budget, kind)
        if totals[kind] > cap:
            over.append((kind, totals[kind], cap))
    if over:
        for kind, total, cap in over:
            suffix = "override approved." if trip.caps_overridden else "override approval required."
            sentences.append(
                f"{CATEGORY_LABELS[kind]} exceeds its cap by {trip.components[0].currency if trip.components else 'USD'} "
                f"{money_str(total - cap)} — {suffix}")
    else:
        sentences.append("All components within policy caps.")
    limit = tx_limit_for(adapter)
    if limit is not None:
        blocked = [c for c in trip.components
                   if c.currency == "USD" and Decimal(c.amount) > limit and c.status != "PURCHASED"]
        if len(blocked) == 1:
            comp = blocked[0]
            sentences.append(
                f"One component ({comp.label}, {comp.currency} {money_str(comp.amount)}) exceeds the "
                f"active credential limit of {comp.currency} {_fmt_limit(limit)} and cannot execute yet.")
        elif len(blocked) > 1:
            names = ", ".join(f"{c.label} ({c.currency} {money_str(c.amount)})" for c in blocked)
            sentences.append(
                f"{len(blocked)} components ({names}) exceed the active credential limit of "
                f"USD {_fmt_limit(limit)} and cannot execute yet.")
    return " ".join(sentences)


def _component_dicts(components) -> list[dict]:
    out = []
    for c in components:
        if isinstance(c, dict):
            out.append({"kind": c["kind"], "label": c["label"], "merchant": c["merchant"],
                        "amount": money_str(c["amount"]), "currency": c["currency"]})
        else:
            out.append({"kind": c.kind, "label": c.label, "merchant": c.merchant,
                        "amount": money_str(c.amount), "currency": c.currency})
    return out


def snapshot_hash(*, employer_id: uuid.UUID | str, contractor_id: uuid.UUID | str | None,
                  contractor_name: str, destination: str, start_date: date, end_date: date,
                  components, caps_overridden: bool, expires_at: datetime) -> str:
    """Hash of the exact approved snapshot. Any change breaks the hash."""
    canonical = json.dumps({
        "employer_id": str(employer_id),
        "contractor_id": str(contractor_id) if contractor_id else None,
        "contractor_name": contractor_name,
        "destination": destination,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "components": _component_dicts(components),
        "caps_overridden": bool(caps_overridden),
        "expires_at": expires_at.isoformat(),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def budget_lock(s: Session, employer_id: uuid.UUID) -> tuple[bool, str | None]:
    if employer_payroll_held(s, employer_id):
        return True, "Payroll is held for this employer — wages are funded before travel spend."
    return False, None


def create_trip(s: Session, *, organization_id: uuid.UUID, employer: Employer,
                contractor: Contractor | None, contractor_name: str | None = None,
                destination: str, start_date: date, end_date: date, purpose: str,
                components: list[dict], created_by_id: uuid.UUID | None = None,
                created_by_email: str | None = None) -> TripRequest:
    name = contractor_name or (contractor.full_name if contractor is not None else None)
    if not name:
        raise TravelError("CONTRACTOR_REQUIRED", "A contractor or contractor name is required.")
    if not components:
        raise TravelError("COMPONENTS_REQUIRED", "A trip needs at least one component.")
    trip = TripRequest(
        organization_id=organization_id, employer_id=employer.id,
        contractor_id=contractor.id if contractor is not None else None,
        contractor_name=name, destination=destination, start_date=start_date,
        end_date=end_date, purpose=purpose, status="REQUESTED", caps_overridden=False,
        created_by_id=created_by_id, created_by_email=created_by_email)
    s.add(trip)
    s.flush()
    for pos, spec in enumerate(components):
        s.add(TripComponent(
            trip_id=trip.id, position=pos, kind=spec["kind"], label=spec["label"],
            merchant=spec["merchant"], amount=round_money(spec["amount"]),
            currency=spec.get("currency", "USD"), status="PENDING"))
    s.flush()
    s.refresh(trip)
    return trip


def _budget_for_trip(s: Session, trip: TripRequest) -> TravelBudget | None:
    currency = trip.components[0].currency if trip.components else "USD"
    return s.scalar(select(TravelBudget).where(TravelBudget.employer_id == trip.employer_id,
                                               TravelBudget.currency == currency))


def decide_trip(s: Session, *, trip: TripRequest, decision: str,
                override_caps: bool = False, user: User,
                reason: str | None = None) -> TripRequest:
    if trip.status != "REQUESTED":
        raise TravelError("TRIP_NOT_PENDING",
                          f"This trip is {trip.status} and can no longer be decided.")
    if decision not in ("approve", "deny"):
        raise TravelError("INVALID_DECISION", "Decision must be 'approve' or 'deny'.")
    now = utcnow()
    trip.decided_by_id = user.id
    trip.decided_by_email = user.email
    trip.decided_at = now
    if decision == "deny":
        trip.status = "DENIED"
        trip.deny_reason = reason
        trip.approval_hash = None
        trip.approval_expires_at = None
        trip.caps_overridden = False
        s.flush()
        return trip
    # approve: per-category cap policy check first.
    budget = _budget_for_trip(s, trip)
    totals = category_totals(trip)
    for kind in ("TRANSPORT", "LODGING", "PER_DIEM", "OTHER"):
        if kind not in totals:
            continue
        cap = category_cap(budget, kind)
        if totals[kind] > cap and not override_caps:
            currency = trip.components[0].currency if trip.components else "USD"
            raise TravelError(
                "POLICY_CAP_EXCEEDED",
                f"{CATEGORY_LABELS[kind]} total {currency} {money_str(totals[kind])} exceeds the "
                f"per-trip {CATEGORY_LABELS[kind]} cap of {currency} {money_str(cap)}. "
                f"Approve with override_caps=true to override.")
    expires = now + timedelta(days=APPROVAL_TTL_DAYS)
    trip.status = "APPROVED"
    trip.caps_overridden = bool(override_caps)
    trip.approval_expires_at = expires
    trip.approval_hash = snapshot_hash(
        employer_id=trip.employer_id, contractor_id=trip.contractor_id,
        contractor_name=trip.contractor_name, destination=trip.destination,
        start_date=trip.start_date, end_date=trip.end_date,
        components=list(trip.components), caps_overridden=trip.caps_overridden,
        expires_at=expires)
    trip.deny_reason = None
    s.flush()
    return trip


def _rollup_trip_status(trip: TripRequest) -> None:
    statuses = [c.status for c in trip.components]
    if statuses and all(st == "PURCHASED" for st in statuses):
        trip.status = "COMPLETED"
    elif any(st == "PURCHASED" for st in statuses):
        trip.status = "PARTIALLY_PURCHASED"


def _validate_trip(trip: TripRequest) -> None:
    if trip.status not in ("APPROVED", "PARTIALLY_PURCHASED"):
        raise TravelError("TRIP_NOT_APPROVED",
                          f"This trip is {trip.status}; only an approved trip can be purchased.")
    if trip.approval_expires_at is None or trip.approval_hash is None:
        raise TravelError("SNAPSHOT_MISMATCH", "This trip has no valid approval snapshot.")
    expires = trip.approval_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    recomputed = snapshot_hash(
        employer_id=trip.employer_id, contractor_id=trip.contractor_id,
        contractor_name=trip.contractor_name, destination=trip.destination,
        start_date=trip.start_date, end_date=trip.end_date,
        components=list(trip.components), caps_overridden=trip.caps_overridden,
        expires_at=expires)
    if recomputed != trip.approval_hash:
        raise TravelError("SNAPSHOT_MISMATCH",
                          "A component, the destination, the dates or the override flag changed "
                          "after approval. A new approval is required.")
    if utcnow() > expires:
        raise TravelError("APPROVAL_EXPIRED", "The approval for this trip has expired. "
                                              "A new approval is required.")


def purchase_component(s: Session, runtime, *, trip: TripRequest, component: TripComponent,
                       user: User | None = None) -> TripPurchase:
    """Execute (or idempotently return) the purchase for one trip component."""
    existing_success = s.scalar(
        select(TripPurchase).where(TripPurchase.component_id == component.id,
                                   TripPurchase.status == "SUCCEEDED")
        .order_by(TripPurchase.attempt_no))
    if existing_success is not None:
        return existing_success
    if component.status == "PURCHASED":
        raise TravelError("SNAPSHOT_MISMATCH", "Component is PURCHASED but has no purchase record.")

    _validate_trip(trip)

    employer = s.get(Employer, trip.employer_id)
    if employer is None:
        raise TravelError("EMPLOYER_NOT_FOUND", "The employer for this trip no longer exists.")

    budget = s.scalar(select(TravelBudget).where(TravelBudget.employer_id == trip.employer_id,
                                                 TravelBudget.currency == component.currency))
    if budget is None or not budget.enabled:
        raise TravelError("BUDGET_NOT_ENABLED",
                          f"No enabled {component.currency} travel budget exists for this employer.")
    if employer_payroll_held(s, trip.employer_id):
        raise TravelError("PAYROLL_HELD", "Payroll for this employer is held; wages are funded before travel spend.")

    period = current_period()
    if budget.period != period:
        budget.spent_this_period = Decimal("0.00")
        budget.period = period
    if Decimal(budget.spent_this_period) + Decimal(component.amount) > Decimal(budget.monthly_cap):
        raise TravelError("MONTHLY_CAP_EXCEEDED",
                          f"This purchase would exceed the {component.currency} monthly travel cap of "
                          f"{money_str(budget.monthly_cap)} (already spent {money_str(budget.spent_this_period)}).")

    attempt_count = s.scalar(
        select(func.count()).select_from(TripPurchase)
        .where(TripPurchase.component_id == component.id)) or 0
    attempt_no = int(attempt_count) + 1
    idempotency_key = f"trip:{component.id}:{attempt_no}"

    try:
        result = runtime.vic.purchase(merchant=component.merchant, amount=Decimal(component.amount),
                                      currency=component.currency, idempotency_key=idempotency_key)
    except VicUnavailable as exc:
        raise TravelError("VIC_UNAVAILABLE", str(exc)) from None

    purchase = TripPurchase(
        organization_id=trip.organization_id, employer_id=trip.employer_id,
        trip_id=trip.id, component_id=component.id, attempt_no=attempt_no,
        idempotency_key=idempotency_key,
        mode=runtime.vic.mode, credential_ref=runtime.vic.credential_ref,
        provider_ref=result.provider_ref,
        status="SUCCEEDED" if result.ok else "FAILED",
        failure_code=result.failure_code, failure_detail=result.failure_detail,
        amount=Decimal(component.amount), currency=component.currency,
        created_by_id=user.id if user is not None else None,
        created_by_email=user.email if user is not None else None)
    s.add(purchase)
    s.flush()

    if result.ok:
        budget.spent_this_period = Decimal(budget.spent_this_period) + Decimal(component.amount)
        component.status = "PURCHASED"
        ccy = component.currency
        journal = runtime.post_employer_journal(
            s, employer,
            [(f"{employer.ledger_key}:travel_expense:{ccy}", Decimal(component.amount)),
             (f"{employer.ledger_key}:travel_budget:{ccy}", -Decimal(component.amount))],
            memo=f"Travel purchase — {component.label} · {component.merchant} (VIC {runtime.vic.mode})")
        purchase.journal_txn_id = journal.id
    else:
        component.status = "PENDING" if result.failure_code == "TX_LIMIT_EXCEEDED" else "FAILED"
    _rollup_trip_status(trip)
    s.flush()
    return purchase


def purchase_all(s: Session, runtime, *, trip: TripRequest,
                 user: User | None = None) -> dict:
    """Attempt each PENDING/FAILED component once. Never re-buys PURCHASED."""
    _validate_trip(trip)
    purchases: list[TripPurchase] = []
    errors: list[dict] = []
    for component in list(trip.components):
        if component.status not in ("PENDING", "FAILED"):
            continue
        try:
            purchases.append(purchase_component(s, runtime, trip=trip, component=component, user=user))
        except TravelError as exc:
            if exc.code in TRIP_LEVEL_CODES:
                raise
            errors.append({"component_id": str(component.id), "code": exc.code, "message": exc.detail})
    s.flush()
    return {"trip": trip, "purchases": purchases, "errors": errors}


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_trip(s: Session, organization_id: uuid.UUID, trip_id: uuid.UUID) -> TripRequest | None:
    return s.scalar(select(TripRequest).where(TripRequest.id == trip_id,
                                              TripRequest.organization_id == organization_id))


def list_trips(s: Session, organization_id: uuid.UUID,
               employer_id: uuid.UUID | None = None) -> list[TripRequest]:
    q = (select(TripRequest).where(TripRequest.organization_id == organization_id)
         .order_by(TripRequest.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(TripRequest.employer_id == employer_id)
    return list(s.scalars(q))


def list_purchases(s: Session, organization_id: uuid.UUID, *,
                   employer_id: uuid.UUID | None = None, trip_id: uuid.UUID | None = None,
                   component_id: uuid.UUID | None = None, status: str | None = None) -> list[TripPurchase]:
    q = (select(TripPurchase).where(TripPurchase.organization_id == organization_id)
         .order_by(TripPurchase.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(TripPurchase.employer_id == employer_id)
    if trip_id:
        q = q.where(TripPurchase.trip_id == trip_id)
    if component_id:
        q = q.where(TripPurchase.component_id == component_id)
    if status:
        q = q.where(TripPurchase.status == status)
    return list(s.scalars(q))
