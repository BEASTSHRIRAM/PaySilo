"""Travel & Accommodation — employer-funded contractor trips via Visa VIC.

Mirrors ``benefits.py`` / ``onboarding.py`` route shapes, role checks and
error mapping (409 ``{detail:{code,message}}``). Org scoping is identical:
another organization's rows are a 404, never a leak. Out models live in
this file. Component Out carries the policy flags (``over_cap``, ``cap``,
``tx_limit_blocked``) and Trip Out carries the deterministic
``policy_summary`` from ``services.travel`` — the same read path the
chat lane uses.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import Contractor, Employer, TravelBudget, TripComponent, TripPurchase, TripRequest
from ...money import money_str
from ...partners.vic import vic_status
from ...services.benefits import current_period
from ...services.runtime import PlatformRuntime
from ...services.travel import (
    TravelError,
    budget_lock,
    cancel_component,
    complete_component,
    component_flags,
    create_trip,
    decide_trip,
    list_purchases,
    list_trips,
    policy_summary,
    purchase_all,
    purchase_component,
    trip_email_context,
)
from ..deps import APPROVER_ROLES, EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from ..schemas import CurrencyCode, VicStatusOut, _money
from .employers import load_employer
from . import spend_hooks as hooks

router = APIRouter(prefix="/travel", tags=["travel"])


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------

class TravelBudgetIn(BaseModel):
    currency: str = CurrencyCode
    monthly_cap: str
    per_tx_limit: str | None = None
    enabled: bool
    cap_transport: str = "750.00"
    cap_lodging: str = "600.00"
    cap_per_diem: str = "300.00"
    cap_other: str = "200.00"

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("monthly_cap", "cap_transport", "cap_lodging", "cap_per_diem", "cap_other")
    @classmethod
    def _cap(cls, v: str) -> str:
        return _money(v)

    @field_validator("per_tx_limit")
    @classmethod
    def _limit(cls, v: str | None) -> str | None:
        return _money(v) if v not in (None, "") else None


class TravelBudgetOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    currency: str
    monthly_cap: str
    per_tx_limit: str | None
    spent_this_period: str
    remaining: str
    period: str
    enabled: bool
    locked: bool
    lock_reason: str | None
    cap_transport: str
    cap_lodging: str
    cap_per_diem: str
    cap_other: str


class TripComponentIn(BaseModel):
    kind: Literal["TRANSPORT", "LODGING", "PER_DIEM", "OTHER"]
    label: str = Field(min_length=1, max_length=300)
    merchant: str = Field(min_length=1, max_length=200)
    amount: str
    currency: str = CurrencyCode

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("label", "merchant")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        return _money(v)


class TripCreateIn(BaseModel):
    employer_id: uuid.UUID
    contractor_id: uuid.UUID | None = None
    contractor_name: str | None = Field(default=None, max_length=120)
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    purpose: str = Field(default="", max_length=500)
    components: list[TripComponentIn] = Field(min_length=1, max_length=50)


class TripDecisionIn(BaseModel):
    decision: Literal["approve", "deny"]
    override_caps: bool = False
    reason: str | None = Field(default=None, max_length=2000)


class TripPurchaseOut(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    component_id: uuid.UUID
    employer_id: uuid.UUID
    attempt_no: int
    idempotency_key: str
    mode: str
    credential_ref: str
    provider_ref: str | None
    status: str
    failure_code: str | None
    failure_detail: str | None
    amount: str
    currency: str
    journal_txn_id: int | None
    created_at: datetime
    provider_mandate_id: str | None = None
    approval_url: str | None = None
    mandate_status: str | None = None


class TripCompleteIn(BaseModel):
    purchase_confirmed: bool = False


class TripComponentOut(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    kind: str
    label: str
    merchant: str
    amount: str
    currency: str
    status: str
    failure_code: str | None
    failure_detail: str | None
    over_cap: bool
    cap: str
    tx_limit_blocked: bool
    latest_purchase: TripPurchaseOut | None


class TripOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    contractor_id: str | None
    contractor_name: str
    destination: str
    start_date: str
    end_date: str
    purpose: str
    status: str
    caps_overridden: bool
    policy_summary: str
    approval_hash: str | None
    approval_expires_at: str | None
    decided_by_email: str | None
    decided_at: str | None
    deny_reason: str | None
    created_by_email: str | None
    created_at: datetime
    components: list[TripComponentOut]


class PurchaseAllErrorOut(BaseModel):
    component_id: str
    code: str
    message: str


class TripPurchaseAllOut(BaseModel):
    trip: TripOut
    purchases: list[TripPurchaseOut]
    errors: list[PurchaseAllErrorOut]


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def trip_purchase_out(p: TripPurchase) -> TripPurchaseOut:
    return TripPurchaseOut(
        id=p.id, trip_id=p.trip_id, component_id=p.component_id, employer_id=p.employer_id,
        attempt_no=p.attempt_no, idempotency_key=p.idempotency_key, mode=p.mode, credential_ref=p.credential_ref,
        provider_ref=p.provider_ref, status=p.status, failure_code=p.failure_code,
        failure_detail=p.failure_detail, amount=money_str(p.amount), currency=p.currency,
        journal_txn_id=p.journal_txn_id, created_at=p.created_at,
        provider_mandate_id=p.provider_mandate_id, approval_url=p.approval_url, mandate_status=p.mandate_status)


def _budget_for(db: Session, trip: TripRequest, component: TripComponent | None = None) -> TravelBudget | None:
    currency = component.currency if component else (trip.components[0].currency if trip.components else "USD")
    return db.scalar(select(TravelBudget).where(TravelBudget.employer_id == trip.employer_id,
                                                TravelBudget.currency == currency))


def trip_component_out(db: Session, trip: TripRequest, comp: TripComponent,
                       adapter=None) -> TripComponentOut:
    latest = db.scalar(
        select(TripPurchase).where(TripPurchase.component_id == comp.id)
        .order_by(TripPurchase.attempt_no.desc()))
    flags = component_flags(trip, comp, _budget_for(db, trip, comp), adapter)
    return TripComponentOut(
        id=comp.id, trip_id=comp.trip_id, kind=comp.kind, label=comp.label,
        merchant=comp.merchant, amount=money_str(comp.amount), currency=comp.currency,
        status=comp.status,
        failure_code=latest.failure_code if latest and latest.status == "FAILED" else None,
        failure_detail=latest.failure_detail if latest and latest.status == "FAILED" else None,
        over_cap=flags["over_cap"], cap=flags["cap"], tx_limit_blocked=flags["tx_limit_blocked"],
        latest_purchase=trip_purchase_out(latest) if latest else None)


def trip_out(db: Session, trip: TripRequest, employer_name: str = "", adapter=None) -> TripOut:
    if not employer_name:
        employer_name = db.scalar(select(Employer.name).where(Employer.id == trip.employer_id)) or ""
    budget = _budget_for(db, trip)
    return TripOut(
        id=trip.id, employer_id=trip.employer_id, employer_name=employer_name,
        contractor_id=str(trip.contractor_id) if trip.contractor_id else None,
        contractor_name=trip.contractor_name, destination=trip.destination,
        start_date=trip.start_date.isoformat(), end_date=trip.end_date.isoformat(),
        purpose=trip.purpose, status=trip.status, caps_overridden=trip.caps_overridden,
        policy_summary=policy_summary(trip, budget, adapter),
        approval_hash=trip.approval_hash,
        approval_expires_at=trip.approval_expires_at.isoformat() if trip.approval_expires_at else None,
        decided_by_email=trip.decided_by_email,
        decided_at=trip.decided_at.isoformat() if trip.decided_at else None,
        deny_reason=trip.deny_reason, created_by_email=trip.created_by_email,
        created_at=trip.created_at,
        components=[trip_component_out(db, trip, c, adapter) for c in trip.components])


def travel_budget_out(db: Session, b: TravelBudget, employer: Employer) -> TravelBudgetOut:
    locked, lock_reason = budget_lock(db, employer.id)
    return TravelBudgetOut(
        id=b.id, employer_id=b.employer_id, employer_name=employer.name, currency=b.currency,
        monthly_cap=money_str(b.monthly_cap),
        per_tx_limit=money_str(b.per_tx_limit) if b.per_tx_limit is not None else None,
        spent_this_period=money_str(b.spent_this_period),
        remaining=money_str(b.monthly_cap - b.spent_this_period),
        period=b.period, enabled=b.enabled, locked=locked, lock_reason=lock_reason,
        cap_transport=money_str(b.cap_transport), cap_lodging=money_str(b.cap_lodging),
        cap_per_diem=money_str(b.cap_per_diem), cap_other=money_str(b.cap_other))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(exc: TravelError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.detail})


def _load_trip(db: Session, tenant: Tenant, trip_id: uuid.UUID) -> TripRequest:
    trip = db.scalar(select(TripRequest).where(
        TripRequest.id == trip_id, TripRequest.organization_id == tenant.organization.id))
    if trip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found.")
    return trip


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=VicStatusOut)
def get_status(tenant: Tenant = Depends(current_tenant),
               runtime: PlatformRuntime = Depends(get_runtime),
               settings: Settings = Depends(get_settings)) -> VicStatusOut:
    return VicStatusOut(**vic_status(settings, runtime.vic))


@router.get("/budgets", response_model=list[TravelBudgetOut])
def list_budgets(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[TravelBudgetOut]:
    rows = db.execute(
        select(TravelBudget, Employer)
        .join(Employer, TravelBudget.employer_id == Employer.id)
        .where(TravelBudget.organization_id == tenant.organization.id)
        .order_by(Employer.name, TravelBudget.currency)).all()
    return [travel_budget_out(db, b, e) for b, e in rows]


@router.put("/budgets/{employer_id}", response_model=TravelBudgetOut)
def upsert_budget(employer_id: uuid.UUID, body: TravelBudgetIn,
                  tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                  db: Session = Depends(get_db)) -> TravelBudgetOut:
    emp = load_employer(db, tenant, employer_id)
    budget = db.scalar(select(TravelBudget).where(TravelBudget.employer_id == emp.id,
                                                  TravelBudget.currency == body.currency))
    if budget is None:
        budget = TravelBudget(organization_id=tenant.organization.id, employer_id=emp.id,
                              currency=body.currency, monthly_cap=Decimal(body.monthly_cap),
                              per_tx_limit=Decimal(body.per_tx_limit) if body.per_tx_limit else None,
                              spent_this_period=Decimal("0.00"), period=current_period(),
                              enabled=body.enabled,
                              cap_transport=Decimal(body.cap_transport), cap_lodging=Decimal(body.cap_lodging),
                              cap_per_diem=Decimal(body.cap_per_diem), cap_other=Decimal(body.cap_other))
        db.add(budget)
    else:
        budget.monthly_cap = Decimal(body.monthly_cap)
        budget.per_tx_limit = Decimal(body.per_tx_limit) if body.per_tx_limit else None
        budget.enabled = body.enabled
        budget.cap_transport = Decimal(body.cap_transport)
        budget.cap_lodging = Decimal(body.cap_lodging)
        budget.cap_per_diem = Decimal(body.cap_per_diem)
        budget.cap_other = Decimal(body.cap_other)
    db.commit()
    db.refresh(budget)
    return travel_budget_out(db, budget, emp)


@router.get("/trips", response_model=list[TripOut])
def list_trip_route(employer_id: uuid.UUID | None = None,
                    tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                    runtime: PlatformRuntime = Depends(get_runtime)) -> list[TripOut]:
    return [trip_out(db, t, adapter=runtime.vic) for t in list_trips(db, tenant.organization.id, employer_id)]


@router.post("/trips", response_model=TripOut, status_code=status.HTTP_201_CREATED)
def create_trip_route(request: Request, body: TripCreateIn,
                      tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                      db: Session = Depends(get_db),
                      runtime: PlatformRuntime = Depends(get_runtime)) -> TripOut:
    employer = load_employer(db, tenant, body.employer_id)
    contractor = None
    if body.contractor_id is not None:
        contractor = next((c for c in employer.contractors if c.id == body.contractor_id), None)
        if contractor is None:
            raise HTTPException(422, f"Contractor {body.contractor_id} does not belong to {employer.name}.")
    if contractor is None and not (body.contractor_name and body.contractor_name.strip()):
        raise HTTPException(422, "contractor_name is required when no contractor_id is given.")
    if body.end_date < body.start_date:
        raise HTTPException(422, "end_date must not be before start_date.")
    try:
        trip = create_trip(
            db, organization_id=tenant.organization.id, employer=employer, contractor=contractor,
            contractor_name=body.contractor_name, destination=body.destination,
            start_date=body.start_date, end_date=body.end_date, purpose=body.purpose,
            components=[{"kind": c.kind, "label": c.label, "merchant": c.merchant,
                         "amount": Decimal(c.amount), "currency": c.currency} for c in body.components],
            created_by_id=tenant.user.id, created_by_email=tenant.user.email)
    except TravelError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.detail}) from None
    db.commit()
    db.refresh(trip)
    hooks.dispatch(request, db, tenant, "trip.proposed", "trip", trip.id,
                   {"employer_id": str(trip.employer_id), "contractor_name": trip.contractor_name,
                    "destination": trip.destination, "status": trip.status})
    hooks.notify_deciders(
        request, db, tenant, entity_key=f"trip:{trip.id}", employer_id=employer.id,
        context={"entity_label": f"Trip to {trip.destination} for {trip.contractor_name}",
                 "employer_name": employer.name, "requested_for": trip.contractor_name,
                 "lines": [{"label": c.label, "amount": str(c.amount), "currency": c.currency}
                           for c in trip.components],
                 "total": str(sum((c.amount for c in trip.components), Decimal("0"))),
                 "currency": trip.components[0].currency if trip.components else "USD",
                 "deep_link": "/dashboard/travel"})
    return trip_out(db, trip, employer.name, adapter=runtime.vic)


@router.post("/trips/{trip_id}/decision", response_model=TripOut)
def decide_trip_route(request: Request, trip_id: uuid.UUID, body: TripDecisionIn,
                      tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                      db: Session = Depends(get_db),
                      runtime: PlatformRuntime = Depends(get_runtime)) -> TripOut:
    trip = _load_trip(db, tenant, trip_id)
    try:
        decide_trip(db, trip=trip, decision=body.decision, override_caps=body.override_caps,
                    user=tenant.user, reason=body.reason)
    except TravelError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(trip)
    hooks.resolve_gate(request, db, tenant, "trip", trip.id,
                       "APPROVED" if body.decision == "approve" else "REJECTED", tenant.user)
    return trip_out(db, trip, adapter=runtime.vic)


def _trip_itinerary(request: Request, db: Session, tenant: Tenant, trip: TripRequest) -> None:
    """Mail the contractor the itinerary with per-component status, including the honest BLOCKED line for an
    over-limit component. Queued through the platform outbox; the dedupe key covers the component states, so
    a repeated click never sends the same itinerary twice but a changed state does."""
    if trip.contractor_id is None:
        return
    contractor = db.get(Contractor, trip.contractor_id)
    if contractor is None or contractor.organization_id != tenant.organization.id or not contractor.email:
        return
    context = trip_email_context(db, trip)
    state = "".join(f"{c['label']}:{c['status']};" for c in context["components"])
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]
    hooks.queue_itinerary(request, db, tenant, "trip_itinerary", contractor.email, context,
                          dedupe_key=f"trip-itinerary:{trip.id}:{digest}", employer_id=trip.employer_id,
                          contractor_id=contractor.id)


def _itinerary_if_settled(request: Request, db: Session, tenant: Tenant, trip: TripRequest) -> None:
    if any(c.status == "PURCHASED" for c in trip.components):
        _trip_itinerary(request, db, tenant, trip)


@router.post("/trips/{trip_id}/components/{component_id}/purchase", response_model=TripPurchaseOut)
def purchase_component_route(request: Request, trip_id: uuid.UUID, component_id: uuid.UUID,
                             tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                             db: Session = Depends(get_db),
                             runtime: PlatformRuntime = Depends(get_runtime)) -> TripPurchaseOut:
    trip = _load_trip(db, tenant, trip_id)
    component = next((c for c in trip.components if c.id == component_id), None)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip component not found.")
    try:
        purchase = purchase_component(db, runtime, trip=trip, component=component, user=tenant.user)
    except TravelError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(purchase)
    db.refresh(trip)
    _itinerary_if_settled(request, db, tenant, trip)
    return trip_purchase_out(purchase)


@router.post("/trips/{trip_id}/purchase-all", response_model=TripPurchaseAllOut)
def purchase_all_route(request: Request, trip_id: uuid.UUID,
                       tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                       db: Session = Depends(get_db),
                       runtime: PlatformRuntime = Depends(get_runtime)) -> TripPurchaseAllOut:
    trip = _load_trip(db, tenant, trip_id)
    try:
        result = purchase_all(db, runtime, trip=trip, user=tenant.user)
    except TravelError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(trip)
    _itinerary_if_settled(request, db, tenant, trip)
    return TripPurchaseAllOut(
        trip=trip_out(db, trip, adapter=runtime.vic),
        purchases=[trip_purchase_out(p) for p in result["purchases"]],
        errors=[PurchaseAllErrorOut(**e) for e in result["errors"]])


def _load_purchase(db: Session, tenant: Tenant, purchase_id: uuid.UUID) -> tuple[TripPurchase, TripRequest, TripComponent]:
    purchase = db.scalar(select(TripPurchase).where(TripPurchase.id == purchase_id,
                                                    TripPurchase.organization_id == tenant.organization.id))
    if purchase is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip purchase not found.")
    trip = _load_trip(db, tenant, purchase.trip_id)
    component = next((c for c in trip.components if c.id == purchase.component_id), None)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip component not found.")
    return purchase, trip, component


@router.post("/purchases/{purchase_id}/complete", response_model=TripPurchaseOut)
def complete_purchase_route(request: Request, purchase_id: uuid.UUID, body: TripCompleteIn,
                            tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                            db: Session = Depends(get_db),
                            runtime: PlatformRuntime = Depends(get_runtime)) -> TripPurchaseOut:
    """Phase B of a live VIC purchase: an explicit human click (never the LLM)."""
    purchase, trip, component = _load_purchase(db, tenant, purchase_id)
    try:
        complete_component(db, runtime, trip=trip, component=component, purchase=purchase,
                           purchase_confirmed=body.purchase_confirmed)
    except TravelError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(purchase)
    db.refresh(trip)
    _itinerary_if_settled(request, db, tenant, trip)
    return trip_purchase_out(purchase)


@router.post("/purchases/{purchase_id}/cancel", response_model=TripPurchaseOut)
def cancel_purchase_route(purchase_id: uuid.UUID, tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                          db: Session = Depends(get_db),
                          runtime: PlatformRuntime = Depends(get_runtime)) -> TripPurchaseOut:
    purchase, trip, component = _load_purchase(db, tenant, purchase_id)
    try:
        cancel_component(db, runtime, trip=trip, component=component, purchase=purchase)
    except TravelError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(purchase)
    return trip_purchase_out(purchase)


@router.get("/purchases", response_model=list[TripPurchaseOut])
def list_purchase_route(employer_id: uuid.UUID | None = None, trip_id: uuid.UUID | None = None,
                        component_id: uuid.UUID | None = None,
                        status_filter: str | None = Query(None, alias="status"),
                        tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[TripPurchaseOut]:
    return [trip_purchase_out(p) for p in list_purchases(
        db, tenant.organization.id, employer_id=employer_id, trip_id=trip_id,
        component_id=component_id, status=status_filter)]
