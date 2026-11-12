"""Onboarding Kit — employer-funded new-joiner kits purchased via Visa VIC.

Mirrors ``benefits.py`` route shapes, role checks and error mapping
(409 ``{detail:{code,message}}``). Org scoping is identical: another
organization's rows are a 404, never a leak. Out models live in this
file (kit Out includes items[]; item Out includes status + last failure).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import Employer, KitItem, KitPurchase, OnboardingBudget, OnboardingKit
from ...money import money_str
from ...partners.vic import vic_status
from ...services.benefits import current_period
from ...services.onboarding import (
    OnboardingError,
    budget_lock,
    create_kit,
    decide_kit,
    list_kits,
    list_purchases,
    purchase_all,
    purchase_item,
)
from ...services.runtime import PlatformRuntime
from ..deps import APPROVER_ROLES, EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from ..schemas import CurrencyCode, VicStatusOut, _money
from .employers import load_employer
from .spend_hooks import dispatch as _wf_dispatch
from .spend_hooks import notify_deciders as _wf_notify_deciders
from .spend_hooks import resolve_gate as _wf_resolve_gate

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ---------------------------------------------------------------------------
# Wire models (mirror benefits schemas)
# ---------------------------------------------------------------------------

class OnboardingBudgetIn(BaseModel):
    currency: str = CurrencyCode
    monthly_cap: str
    per_tx_limit: str | None = None
    enabled: bool

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("monthly_cap")
    @classmethod
    def _cap(cls, v: str) -> str:
        return _money(v)

    @field_validator("per_tx_limit")
    @classmethod
    def _limit(cls, v: str | None) -> str | None:
        return _money(v) if v not in (None, "") else None


class OnboardingBudgetOut(BaseModel):
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


class KitItemIn(BaseModel):
    kind: Literal["BACKGROUND_CHECK", "SAAS_SEAT", "GIFT_CARD", "PERIPHERAL", "CUSTOM"]
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


class KitCreateIn(BaseModel):
    employer_id: uuid.UUID
    contractor_id: uuid.UUID | None = None
    contractor_name: str | None = Field(default=None, max_length=120)
    items: list[KitItemIn] = Field(min_length=1, max_length=50)
    source: Literal["MANUAL", "CHAT", "WORKFLOW"] = "MANUAL"
    draft_reason: str | None = Field(default=None, max_length=2000)


class KitDecisionIn(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = Field(default=None, max_length=2000)


class KitPurchaseOut(BaseModel):
    id: uuid.UUID
    kit_id: uuid.UUID
    item_id: uuid.UUID
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


class KitItemOut(BaseModel):
    id: uuid.UUID
    kit_id: uuid.UUID
    kind: str
    label: str
    merchant: str
    amount: str
    currency: str
    status: str
    failure_code: str | None
    failure_detail: str | None
    latest_purchase: KitPurchaseOut | None


class KitOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    contractor_id: str | None
    contractor_name: str
    status: str
    source: str
    draft_reason: str | None
    approval_hash: str | None
    approval_expires_at: str | None
    decided_by_email: str | None
    decided_at: str | None
    deny_reason: str | None
    created_by_email: str | None
    created_at: datetime
    items: list[KitItemOut]


class PurchaseAllErrorOut(BaseModel):
    item_id: str
    code: str
    message: str


class KitPurchaseAllOut(BaseModel):
    kit: KitOut
    purchases: list[KitPurchaseOut]
    errors: list[PurchaseAllErrorOut]


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def kit_purchase_out(p: KitPurchase) -> KitPurchaseOut:
    return KitPurchaseOut(
        id=p.id, kit_id=p.kit_id, item_id=p.item_id, employer_id=p.employer_id,
        attempt_no=p.attempt_no, idempotency_key=p.idempotency_key, mode=p.mode, credential_ref=p.credential_ref,
        provider_ref=p.provider_ref, status=p.status, failure_code=p.failure_code,
        failure_detail=p.failure_detail, amount=money_str(p.amount), currency=p.currency,
        journal_txn_id=p.journal_txn_id, created_at=p.created_at)


def kit_item_out(db: Session, item: KitItem) -> KitItemOut:
    latest = db.scalar(
        select(KitPurchase).where(KitPurchase.item_id == item.id)
        .order_by(KitPurchase.attempt_no.desc()))
    return KitItemOut(
        id=item.id, kit_id=item.kit_id, kind=item.kind, label=item.label,
        merchant=item.merchant, amount=money_str(item.amount), currency=item.currency,
        status=item.status,
        failure_code=latest.failure_code if latest and latest.status == "FAILED" else None,
        failure_detail=latest.failure_detail if latest and latest.status == "FAILED" else None,
        latest_purchase=kit_purchase_out(latest) if latest else None)


def kit_out(db: Session, kit: OnboardingKit, employer_name: str = "") -> KitOut:
    if not employer_name:
        employer_name = db.scalar(select(Employer.name).where(Employer.id == kit.employer_id)) or ""
    return KitOut(
        id=kit.id, employer_id=kit.employer_id, employer_name=employer_name,
        contractor_id=str(kit.contractor_id) if kit.contractor_id else None,
        contractor_name=kit.contractor_name, status=kit.status,
        source=kit.source, draft_reason=kit.draft_reason,
        approval_hash=kit.approval_hash,
        approval_expires_at=kit.approval_expires_at.isoformat() if kit.approval_expires_at else None,
        decided_by_email=kit.decided_by_email,
        decided_at=kit.decided_at.isoformat() if kit.decided_at else None,
        deny_reason=kit.deny_reason, created_by_email=kit.created_by_email,
        created_at=kit.created_at,
        items=[kit_item_out(db, i) for i in kit.items])


def onboarding_budget_out(db: Session, b: OnboardingBudget, employer: Employer) -> OnboardingBudgetOut:
    locked, lock_reason = budget_lock(db, employer.id)
    return OnboardingBudgetOut(
        id=b.id, employer_id=b.employer_id, employer_name=employer.name, currency=b.currency,
        monthly_cap=money_str(b.monthly_cap),
        per_tx_limit=money_str(b.per_tx_limit) if b.per_tx_limit is not None else None,
        spent_this_period=money_str(b.spent_this_period),
        remaining=money_str(b.monthly_cap - b.spent_this_period),
        period=b.period, enabled=b.enabled, locked=locked, lock_reason=lock_reason)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(exc: OnboardingError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.detail})


def _load_kit(db: Session, tenant: Tenant, kit_id: uuid.UUID) -> OnboardingKit:
    kit = db.scalar(select(OnboardingKit).where(
        OnboardingKit.id == kit_id, OnboardingKit.organization_id == tenant.organization.id))
    if kit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Onboarding kit not found.")
    return kit


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=VicStatusOut)
def get_status(tenant: Tenant = Depends(current_tenant),
               runtime: PlatformRuntime = Depends(get_runtime),
               settings: Settings = Depends(get_settings)) -> VicStatusOut:
    return VicStatusOut(**vic_status(settings, runtime.vic))


@router.get("/budgets", response_model=list[OnboardingBudgetOut])
def list_budgets(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[OnboardingBudgetOut]:
    rows = db.execute(
        select(OnboardingBudget, Employer)
        .join(Employer, OnboardingBudget.employer_id == Employer.id)
        .where(OnboardingBudget.organization_id == tenant.organization.id)
        .order_by(Employer.name, OnboardingBudget.currency)).all()
    return [onboarding_budget_out(db, b, e) for b, e in rows]


@router.put("/budgets/{employer_id}", response_model=OnboardingBudgetOut)
def upsert_budget(employer_id: uuid.UUID, body: OnboardingBudgetIn,
                  tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                  db: Session = Depends(get_db)) -> OnboardingBudgetOut:
    emp = load_employer(db, tenant, employer_id)
    budget = db.scalar(select(OnboardingBudget).where(OnboardingBudget.employer_id == emp.id,
                                                      OnboardingBudget.currency == body.currency))
    if budget is None:
        budget = OnboardingBudget(organization_id=tenant.organization.id, employer_id=emp.id,
                                  currency=body.currency, monthly_cap=Decimal(body.monthly_cap),
                                  per_tx_limit=Decimal(body.per_tx_limit) if body.per_tx_limit else None,
                                  spent_this_period=Decimal("0.00"), period=current_period(),
                                  enabled=body.enabled)
        db.add(budget)
    else:
        budget.monthly_cap = Decimal(body.monthly_cap)
        budget.per_tx_limit = Decimal(body.per_tx_limit) if body.per_tx_limit else None
        budget.enabled = body.enabled
    db.commit()
    db.refresh(budget)
    return onboarding_budget_out(db, budget, emp)


@router.get("/kits", response_model=list[KitOut])
def list_kit_route(employer_id: uuid.UUID | None = None,
                   tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[KitOut]:
    return [kit_out(db, k) for k in list_kits(db, tenant.organization.id, employer_id)]


@router.post("/kits", response_model=KitOut, status_code=status.HTTP_201_CREATED)
def create_kit_route(body: KitCreateIn,
                     tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                     db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> KitOut:
    employer = load_employer(db, tenant, body.employer_id)
    contractor = None
    if body.contractor_id is not None:
        contractor = next((c for c in employer.contractors if c.id == body.contractor_id), None)
        if contractor is None:
            raise HTTPException(422, f"Contractor {body.contractor_id} does not belong to {employer.name}.")
    if contractor is None and not (body.contractor_name and body.contractor_name.strip()):
        raise HTTPException(422, "contractor_name is required when no contractor_id is given.")
    try:
        kit = create_kit(
            db, organization_id=tenant.organization.id, employer=employer, contractor=contractor,
            contractor_name=body.contractor_name,
            items=[{"kind": i.kind, "label": i.label, "merchant": i.merchant,
                    "amount": Decimal(i.amount), "currency": i.currency} for i in body.items],
            created_by_id=tenant.user.id, created_by_email=tenant.user.email,
            source=body.source, draft_reason=body.draft_reason)
    except OnboardingError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.detail}) from None
    db.commit()
    db.refresh(kit)
    _wf_notify_deciders(
        db, settings, tenant,
        context={"entity_label": f"Onboarding kit for {kit.contractor_name}",
                 "employer_name": employer.name, "requested_for": kit.contractor_name,
                 "lines": [{"label": i.label, "amount": str(i.amount), "currency": i.currency}
                           for i in kit.items],
                 "total": str(sum((i.amount for i in kit.items), Decimal("0"))),
                 "currency": kit.items[0].currency if kit.items else "USD",
                 "deep_link": "/dashboard/onboarding"},
        related_kind="kit", related_id=kit.id, related_label=kit.contractor_name)
    return kit_out(db, kit, employer.name)


@router.post("/kits/{kit_id}/decision", response_model=KitOut)
def decide_kit_route(kit_id: uuid.UUID, body: KitDecisionIn,
                     tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                     db: Session = Depends(get_db)) -> KitOut:
    kit = _load_kit(db, tenant, kit_id)
    try:
        decide_kit(db, kit=kit, decision=body.decision, user=tenant.user, reason=body.reason)
    except OnboardingError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(kit)
    _wf_resolve_gate(db, tenant, "kit", kit.id,
                     "APPROVED" if body.decision == "approve" else "REJECTED", tenant.user)
    if body.decision == "approve":
        _wf_dispatch(db, tenant, "kit.approved", "kit", kit.id,
                     {"employer_id": str(kit.employer_id), "contractor_name": kit.contractor_name,
                      "status": kit.status})
    return kit_out(db, kit)


def _kit_completed(db: Session, tenant: Tenant, kit: OnboardingKit) -> None:
    """Kit reached COMPLETED: fire the kit.completed event. The contractor's
    kit_completed email is sent by the seeded Kit Completion Notice workflow
    reacting to that event — deliberately NOT enqueued here as well, so the
    contractor gets exactly one completion email."""
    _wf_dispatch(db, tenant, "kit.completed", "kit", kit.id,
                 {"employer_id": str(kit.employer_id), "contractor_name": kit.contractor_name,
                  "status": kit.status})


@router.post("/kits/{kit_id}/items/{item_id}/purchase", response_model=KitPurchaseOut)
def purchase_item_route(kit_id: uuid.UUID, item_id: uuid.UUID,
                        tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                        db: Session = Depends(get_db),
                        runtime: PlatformRuntime = Depends(get_runtime),
                        settings: Settings = Depends(get_settings)) -> KitPurchaseOut:
    kit = _load_kit(db, tenant, kit_id)
    item = next((i for i in kit.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Kit item not found.")
    try:
        purchase = purchase_item(db, runtime, kit=kit, item=item, user=tenant.user)
    except OnboardingError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(purchase)
    if kit.status == "COMPLETED":
        _kit_completed(db, tenant, kit)
    return kit_purchase_out(purchase)


@router.post("/kits/{kit_id}/purchase-all", response_model=KitPurchaseAllOut)
def purchase_all_route(kit_id: uuid.UUID,
                       tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                       db: Session = Depends(get_db),
                       runtime: PlatformRuntime = Depends(get_runtime),
                       settings: Settings = Depends(get_settings)) -> KitPurchaseAllOut:
    kit = _load_kit(db, tenant, kit_id)
    try:
        result = purchase_all(db, runtime, kit=kit, user=tenant.user)
    except OnboardingError as exc:
        db.rollback()
        raise _error(exc) from None
    db.commit()
    db.refresh(kit)
    if kit.status == "COMPLETED":
        _kit_completed(db, tenant, kit)
    return KitPurchaseAllOut(
        kit=kit_out(db, kit),
        purchases=[kit_purchase_out(p) for p in result["purchases"]],
        errors=[PurchaseAllErrorOut(**e) for e in result["errors"]])


@router.get("/purchases", response_model=list[KitPurchaseOut])
def list_purchase_route(employer_id: uuid.UUID | None = None, kit_id: uuid.UUID | None = None,
                        item_id: uuid.UUID | None = None,
                        status_filter: str | None = Query(None, alias="status"),
                        tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[KitPurchaseOut]:
    return [kit_purchase_out(p) for p in list_purchases(
        db, tenant.organization.id, employer_id=employer_id, kit_id=kit_id,
        item_id=item_id, status=status_filter)]
