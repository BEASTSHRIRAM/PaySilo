"""Silo Benefits — employer-funded benefits purchased via Visa VIC.

Budgets, benefit requests, human decisions on the exact purchase
snapshot, and idempotent purchases. Org scoping is exactly like
``runs.py``: every query filters by the caller's organization, and
another organization's rows are a 404, never a leak.

Audit note: AgentEvent is run-scoped (``run_id`` NOT NULL), so benefits
cannot use it — the append-only BenefitPurchase rows plus
``decided_by`` / ``decided_at`` on requests ARE the audit trail. This
module does not force benefits into ``agent_events``.

All purchases run in SIMULATED mode (labelled everywhere) unless
VIC_MODE=live and the real airi-cli is installed and signed in; the live
adapter is two-phase (mandate -> human authorizes in Airi -> explicit
"complete" click), fails closed and never falls back to simulation.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import BenefitBudget, BenefitPurchase, BenefitRequest, Employer
from ...partners.vic import AiriVicAdapter, VicError, VicUnavailable, vic_status
from ...services.benefits import (
    BenefitError,
    cancel_purchase,
    complete_purchase,
    create_request,
    current_period,
    decide_request,
    execute_purchase,
)
from ...services.runtime import PlatformRuntime
from ..deps import APPROVER_ROLES, EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from ...config import Settings
from ..schemas import (
    BenefitBudgetIn,
    BenefitBudgetOut,
    BenefitDecisionIn,
    BenefitCompleteIn,
    BenefitPurchaseOut,
    BenefitRequestIn,
    AiriLoginOut,
    AiriStatusOut,
    BenefitRequestOut,
    VicStatusOut,
)
from ..serializers import benefit_budget_out, benefit_purchase_out, benefit_request_out
from . import spend_hooks as hooks
from .employers import load_employer

router = APIRouter(prefix="/benefits", tags=["benefits"])
logger = logging.getLogger(__name__)
audit_log = logging.getLogger("paysilo.audit")


class _ConnectLimiter:
    """Sliding window: ``limit`` connects per ``window`` seconds per organization (in memory)."""

    def __init__(self, limit: int = 5, window: float = 600.0) -> None:
        self.limit, self.window = limit, window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int:
        """Record a hit; returns 0 when allowed, else seconds to wait."""
        with self._lock:
            now = time.monotonic()
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])))
            q.append(now)
            return 0


def _benefit_error(exc: BenefitError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.detail})


def _load_request(db: Session, tenant: Tenant, request_id: uuid.UUID) -> BenefitRequest:
    req = db.scalar(select(BenefitRequest).where(
        BenefitRequest.id == request_id,
        BenefitRequest.organization_id == tenant.organization.id))
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benefit request not found.")
    return req


def _employer_name(db: Session, employer_id: uuid.UUID) -> str:
    name = db.scalar(select(Employer.name).where(Employer.id == employer_id))
    return name or ""


def _request_out(db: Session, req: BenefitRequest) -> BenefitRequestOut:
    latest = db.scalar(
        select(BenefitPurchase).where(BenefitPurchase.request_id == req.id)
        .order_by(BenefitPurchase.attempt_no.desc()))
    return benefit_request_out(req, _employer_name(db, req.employer_id), latest)


@router.get("/status", response_model=VicStatusOut)
def get_status(tenant: Tenant = Depends(current_tenant),
               runtime: PlatformRuntime = Depends(get_runtime),
               settings: Settings = Depends(get_settings)) -> VicStatusOut:
    return VicStatusOut(**vic_status(settings, runtime.vic))


# ---------------------------------------------------------------- Connect Airi
# Server-side sign-in of the airi-cli (one Airi account per server, shared by every
# organization). Owner/admin only. The device code is returned to the caller but never
# logged or audited; the audit line records who connected / disconnected.

def _airi(runtime: PlatformRuntime) -> AiriVicAdapter:
    adapter = runtime.vic
    if not isinstance(adapter, AiriVicAdapter):
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            "code": "VIC_SIMULATED",
            "message": "VIC_MODE is simulated, so there is no Airi sign-in to manage. "
                       "Set VIC_MODE=live on the server to connect Airi."})
    return adapter


def _airi_call(fn, *args):
    try:
        return fn(*args)
    except VicUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"code": "AIRI_CLI_UNAVAILABLE", "message": str(exc)}) from None
    except VicError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            detail={"code": exc.code, "message": exc.detail}) from None


@router.get("/airi/status", response_model=AiriStatusOut)
def airi_status(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                runtime: PlatformRuntime = Depends(get_runtime)) -> AiriStatusOut:
    return AiriStatusOut(**_airi_call(_airi(runtime).auth_status))


@router.post("/airi/connect", response_model=AiriLoginOut)
def airi_connect(request: Request, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                 runtime: PlatformRuntime = Depends(get_runtime)) -> AiriLoginOut:
    adapter = _airi(runtime)
    limiter = getattr(request.app.state, "airi_connect_limiter", None)
    if limiter is None:
        limiter = request.app.state.airi_connect_limiter = _ConnectLimiter()
    wait = limiter.hit(str(tenant.organization.id))
    if wait:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many connect attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    result = _airi_call(adapter.start_login, "PaySilo")
    audit_log.info("airi.connect.started org=%s user=%s state=%s", tenant.organization.id, tenant.user.id,
                   result["state"])
    return AiriLoginOut(**result)


@router.post("/airi/poll", response_model=AiriLoginOut)
def airi_poll(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
              runtime: PlatformRuntime = Depends(get_runtime)) -> AiriLoginOut:
    adapter = _airi(runtime)
    result = _airi_call(adapter.poll_login, 8.0)
    if result["state"] in ("connected", "denied", "expired", "failed"):
        audit_log.info("airi.connect.%s org=%s user=%s", result["state"], tenant.organization.id, tenant.user.id)
    return AiriLoginOut(**result)


@router.post("/airi/disconnect", response_model=AiriLoginOut)
def airi_disconnect(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                    runtime: PlatformRuntime = Depends(get_runtime)) -> AiriLoginOut:
    result = _airi_call(_airi(runtime).logout)
    audit_log.info("airi.disconnected org=%s user=%s", tenant.organization.id, tenant.user.id)
    return AiriLoginOut(**result)


@router.get("/budgets", response_model=list[BenefitBudgetOut])
def list_budgets(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[BenefitBudgetOut]:
    rows = db.execute(
        select(BenefitBudget, Employer)
        .join(Employer, BenefitBudget.employer_id == Employer.id)
        .where(BenefitBudget.organization_id == tenant.organization.id)
        .order_by(Employer.name, BenefitBudget.currency)).all()
    return [benefit_budget_out(db, b, e) for b, e in rows]


@router.put("/budgets/{employer_id}", response_model=BenefitBudgetOut)
def upsert_budget(employer_id: uuid.UUID, body: BenefitBudgetIn,
                  tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                  db: Session = Depends(get_db)) -> BenefitBudgetOut:
    emp = load_employer(db, tenant, employer_id)
    budget = db.scalar(select(BenefitBudget).where(BenefitBudget.employer_id == emp.id,
                                                   BenefitBudget.currency == body.currency))
    if budget is None:
        budget = BenefitBudget(organization_id=tenant.organization.id, employer_id=emp.id,
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
    return benefit_budget_out(db, budget, emp)


@router.get("/requests", response_model=list[BenefitRequestOut])
def list_requests(employer_id: uuid.UUID | None = None, status_filter: str | None = Query(None, alias="status"),
                  tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[BenefitRequestOut]:
    q = (select(BenefitRequest)
         .where(BenefitRequest.organization_id == tenant.organization.id)
         .order_by(BenefitRequest.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(BenefitRequest.employer_id == employer_id)
    if status_filter:
        q = q.where(BenefitRequest.status == status_filter)
    return [_request_out(db, r) for r in db.scalars(q)]


@router.post("/requests", response_model=BenefitRequestOut, status_code=status.HTTP_201_CREATED)
def create_benefit_request(request: Request, body: BenefitRequestIn,
                           tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                           db: Session = Depends(get_db)) -> BenefitRequestOut:
    employer = load_employer(db, tenant, body.employer_id)
    contractor = None
    if body.contractor_id is not None:
        # Contractor resolution mirrors runs.py: only THIS employer's
        # contractors resolve; anyone else's contractor is a 422.
        contractor = next((c for c in employer.contractors if c.id == body.contractor_id), None)
        if contractor is None:
            raise HTTPException(422, f"Contractor {body.contractor_id} does not belong to {employer.name}.")
    if contractor is None and not (body.contractor_name and body.contractor_name.strip()):
        raise HTTPException(422, "contractor_name is required when no contractor_id is given.")
    amount = Decimal(body.amount)
    if amount <= 0:
        raise HTTPException(422, "Amount must be positive.")
    try:
        req = create_request(
            db, organization_id=tenant.organization.id, employer=employer, contractor=contractor,
            contractor_name=body.contractor_name, merchant=body.merchant, item=body.item,
            amount=amount, currency=body.currency,
            requested_by_id=tenant.user.id, requested_by_email=tenant.user.email)
    except BenefitError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.detail}) from None
    db.commit()
    db.refresh(req)
    hooks.dispatch(request, db, tenant, "benefit.requested", "benefit_request", req.id,
                   {"employer_id": str(req.employer_id), "employer_name": employer.name,
                    "contractor_name": req.contractor_name, "merchant": req.merchant, "item": req.item,
                    "amount": str(req.amount), "currency": req.currency, "status": req.status})
    return _request_out(db, req)


@router.post("/requests/{request_id}/decision", response_model=BenefitRequestOut)
def decide_benefit_request(request_id: uuid.UUID, body: BenefitDecisionIn,
                           tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                           db: Session = Depends(get_db)) -> BenefitRequestOut:
    req = _load_request(db, tenant, request_id)
    try:
        decide_request(db, request=req, decision=body.decision, user=tenant.user, reason=body.reason)
    except BenefitError as exc:
        db.rollback()
        raise _benefit_error(exc) from None
    db.commit()
    db.refresh(req)
    return _request_out(db, req)


@router.post("/requests/{request_id}/purchase", response_model=BenefitPurchaseOut)
def purchase_benefit_request(request_id: uuid.UUID,
                             tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                             db: Session = Depends(get_db),
                             runtime: PlatformRuntime = Depends(get_runtime)) -> BenefitPurchaseOut:
    req = _load_request(db, tenant, request_id)
    try:
        purchase = execute_purchase(db, runtime, request=req, user=tenant.user)
    except BenefitError as exc:
        db.rollback()
        raise _benefit_error(exc) from None
    db.commit()
    db.refresh(purchase)
    return benefit_purchase_out(purchase)


def _load_purchase(db: Session, tenant: Tenant, purchase_id: uuid.UUID) -> BenefitPurchase:
    p = db.scalar(select(BenefitPurchase).where(
        BenefitPurchase.id == purchase_id,
        BenefitPurchase.organization_id == tenant.organization.id))
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Benefit purchase not found.")
    return p


@router.post("/purchases/{purchase_id}/complete", response_model=BenefitPurchaseOut)
def complete_benefit_purchase(purchase_id: uuid.UUID, body: BenefitCompleteIn,
                              tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                              db: Session = Depends(get_db),
                              runtime: PlatformRuntime = Depends(get_runtime)) -> BenefitPurchaseOut:
    """Phase B of a live VIC purchase: an explicit human click (never the LLM)."""
    purchase = _load_purchase(db, tenant, purchase_id)
    req = _load_request(db, tenant, purchase.request_id)
    try:
        complete_purchase(db, runtime, request=req, purchase=purchase,
                          purchase_confirmed=body.purchase_confirmed)
    except BenefitError as exc:
        db.rollback()
        raise _benefit_error(exc) from None
    db.commit()
    db.refresh(purchase)
    return benefit_purchase_out(purchase)


@router.post("/purchases/{purchase_id}/cancel", response_model=BenefitPurchaseOut)
def cancel_benefit_purchase(purchase_id: uuid.UUID,
                            tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
                            db: Session = Depends(get_db),
                            runtime: PlatformRuntime = Depends(get_runtime)) -> BenefitPurchaseOut:
    purchase = _load_purchase(db, tenant, purchase_id)
    req = _load_request(db, tenant, purchase.request_id)
    try:
        cancel_purchase(db, runtime, request=req, purchase=purchase)
    except BenefitError as exc:
        db.rollback()
        raise _benefit_error(exc) from None
    db.commit()
    db.refresh(purchase)
    return benefit_purchase_out(purchase)


@router.get("/purchases", response_model=list[BenefitPurchaseOut])
def list_purchases(employer_id: uuid.UUID | None = None, request_id: uuid.UUID | None = None,
                   status_filter: str | None = Query(None, alias="status"),
                   tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[BenefitPurchaseOut]:
    q = (select(BenefitPurchase)
         .where(BenefitPurchase.organization_id == tenant.organization.id)
         .order_by(BenefitPurchase.created_at.desc()).limit(100))
    if employer_id:
        q = q.where(BenefitPurchase.employer_id == employer_id)
    if request_id:
        q = q.where(BenefitPurchase.request_id == request_id)
    if status_filter:
        q = q.where(BenefitPurchase.status == status_filter)
    return [benefit_purchase_out(p) for p in db.scalars(q)]
