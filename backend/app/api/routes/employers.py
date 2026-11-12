"""Employers (silos) and their contractors — always scoped to the caller's organization."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ...airwallex.client import AirwallexError
from ...db.models import Contractor, Employer
from ...services.runtime import PlatformRuntime, RuntimeConflict
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, require_roles
from ..schemas import ContractorIn, ContractorOut, EmployerDetailOut, EmployerIn, EmployerOut, FundIn
from ..serializers import contractor_out, employer_detail_out, employer_out

router = APIRouter(prefix="/employers", tags=["employers"])

# --- spend-workflows dispatch wiring (WF lane) ---------------------------
import logging as _logging

_wf_logger = _logging.getLogger(__name__)


def _dispatch_workflow(db: Session, tenant: Tenant, event: str, kind: str, entity_id, facts: dict) -> None:
    """Best-effort workflow dispatch: a workflow failure never breaks creation."""
    try:
        from ...services.workflows import dispatch_event

        dispatch_event(db, organization_id=tenant.organization.id, event_type=event,
                       entity_kind=kind, entity_id=str(entity_id), facts=facts)
        db.commit()
    except Exception:  # noqa: BLE001
        _wf_logger.exception("workflow dispatch %s for %s %s failed (ignored)", event, kind, entity_id)
        db.rollback()
# --- end spend-workflows dispatch wiring ---------------------------------


def load_employer(db: Session, tenant: Tenant, employer_id: uuid.UUID) -> Employer:
    """The only way routes fetch an employer: by id AND organization. 404 otherwise."""
    emp = db.scalar(select(Employer).options(selectinload(Employer.contractors))
                    .where(Employer.id == employer_id, Employer.organization_id == tenant.organization.id))
    if emp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employer not found.")
    return emp


@router.get("", response_model=list[EmployerOut])
def list_employers(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                   runtime: PlatformRuntime = Depends(get_runtime)) -> list[EmployerOut]:
    rows = db.scalars(select(Employer).options(selectinload(Employer.contractors))
                      .where(Employer.organization_id == tenant.organization.id)
                      .order_by(Employer.created_at)).all()
    return [employer_out(db, runtime, e) for e in rows]


@router.post("", response_model=EmployerDetailOut, status_code=status.HTTP_201_CREATED)
def create_employer(body: EmployerIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                    db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> EmployerDetailOut:
    emp = Employer(organization_id=tenant.organization.id, **body.model_dump())
    db.add(emp)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An employer with this name already exists.") from None
    try:
        runtime.ensure_account(db, emp)  # its own connected account = its own silo
    except AirwallexError as exc:
        # Nothing is saved: the employer only exists once its silo (connected account) does.
        db.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Airwallex could not create this employer's account: {exc}") from None
    db.commit()
    _dispatch_workflow(db, tenant, "employer.created", "employer", emp.id, {
        "employer_id": str(emp.id), "employer_name": emp.name, "contact_email": emp.contact_email,
        "contact_name": emp.contact_name, "currency": emp.funding_currency, "status": emp.status})
    return employer_detail_out(db, runtime, load_employer(db, tenant, emp.id))


@router.get("/{employer_id}", response_model=EmployerDetailOut)
def get_employer(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                 runtime: PlatformRuntime = Depends(get_runtime)) -> EmployerDetailOut:
    return employer_detail_out(db, runtime, load_employer(db, tenant, employer_id))


@router.post("/{employer_id}/fund", response_model=EmployerOut)
def fund_employer(employer_id: uuid.UUID, body: FundIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                  db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> EmployerOut:
    """Simulated deposit into this employer's OWN wallet (sandbox simulation API)."""
    emp = load_employer(db, tenant, employer_id)
    try:
        runtime.fund(db, emp, Decimal(body.amount), reference=body.reference or uuid.uuid4().hex)
    except RuntimeConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.commit()
    return employer_out(db, runtime, emp)


@router.post("/{employer_id}/contractors", response_model=ContractorOut, status_code=status.HTTP_201_CREATED)
def add_contractor(employer_id: uuid.UUID, body: ContractorIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                   db: Session = Depends(get_db)) -> ContractorOut:
    emp = load_employer(db, tenant, employer_id)
    data = body.model_dump()
    data["default_amount"] = Decimal(data["default_amount"]) if data["default_amount"] else None
    contractor = Contractor(organization_id=tenant.organization.id, employer_id=emp.id, **data)
    db.add(contractor)
    db.commit()
    _dispatch_workflow(db, tenant, "contractor.created", "contractor", contractor.id, {
        "contractor_id": str(contractor.id), "contractor_name": contractor.full_name,
        "contractor_email": contractor.email, "employer_id": str(emp.id), "employer_name": emp.name,
        "currency": contractor.currency,
        "amount": str(contractor.default_amount) if contractor.default_amount is not None else None,
        "status": "ACTIVE" if contractor.is_active else "INACTIVE"})
    return contractor_out(contractor)


@router.delete("/{employer_id}/contractors/{contractor_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_contractor(employer_id: uuid.UUID, contractor_id: uuid.UUID,
                          tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db)) -> None:
    """Soft-delete: past runs keep pointing at the contractor."""
    emp = load_employer(db, tenant, employer_id)
    contractor = next((c for c in emp.contractors if c.id == contractor_id), None)
    if contractor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor not found.")
    contractor.is_active = False
    db.commit()
