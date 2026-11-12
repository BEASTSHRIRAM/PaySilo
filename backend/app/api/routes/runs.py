"""Payroll runs: start (agents run until a human is needed), inspect, decide."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...db.models import PayrollRun
from ...services.runtime import HeldLineUnavailable, PlatformRuntime, RuntimeConflict
from ..deps import APPROVER_ROLES, EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, require_roles
from ..schemas import DecisionIn, RunDetailOut, RunIn, RunOut
from ..serializers import run_detail_out, run_out
from . import spend_hooks as hooks
from .employers import load_employer

router = APIRouter(prefix="/runs", tags=["runs"])


def load_run(db: Session, tenant: Tenant, run_id: uuid.UUID) -> PayrollRun:
    run = db.scalar(select(PayrollRun)
                    .options(selectinload(PayrollRun.pay_lines), selectinload(PayrollRun.events),
                             selectinload(PayrollRun.approvals), selectinload(PayrollRun.employer))
                    .where(PayrollRun.id == run_id, PayrollRun.organization_id == tenant.organization.id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return run


@router.get("", response_model=list[RunOut])
def list_runs(employer_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=200),
              tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
              runtime: PlatformRuntime = Depends(get_runtime)) -> list[RunOut]:
    q = (select(PayrollRun).options(selectinload(PayrollRun.employer))
         .where(PayrollRun.organization_id == tenant.organization.id)
         .order_by(PayrollRun.created_at.desc()).limit(limit))
    if employer_id:
        q = q.where(PayrollRun.employer_id == employer_id)
    return [run_out(runtime, r) for r in db.scalars(q)]


@router.post("", response_model=RunDetailOut, status_code=status.HTTP_201_CREATED)
def start_run(body: RunIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db),
              runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    employer = load_employer(db, tenant, body.employer_id)
    if employer.status != "ACTIVE":
        raise HTTPException(status.HTTP_409_CONFLICT, f"{employer.name} is not active.")
    pay_lines = None
    if body.lines:
        by_id = {c.id: c for c in employer.contractors if c.is_active}
        pay_lines, seen = [], set()
        for line in body.lines:
            contractor = by_id.get(line.contractor_id)  # only THIS employer's contractors resolve
            if contractor is None:
                raise HTTPException(422,
                                    f"Contractor {line.contractor_id} does not belong to {employer.name}.")
            if contractor.id in seen:
                raise HTTPException(422,
                                    f"{contractor.full_name} appears twice in this run.")
            seen.add(contractor.id)
            amount = line.amount or (str(contractor.default_amount) if contractor.default_amount else None)
            if amount is None:
                raise HTTPException(422,
                                    f"No amount given for {contractor.full_name} and no default amount on file.")
            pay_lines.append(runtime.contractor_line(contractor, amount))
    try:
        run = runtime.start_run(db, org=tenant.organization, user=tenant.user, employer=employer,
                                pay_lines=pay_lines, raw_input=body.instruction if not body.lines else None,
                                source="manual" if body.lines else "text", period_label=body.period_label)
    except RuntimeConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.expire_all()
    return run_detail_out(db, runtime, load_run(db, tenant, run.id))


@router.post("/{run_id}/pay-held", response_model=RunDetailOut, status_code=status.HTTP_201_CREATED)
def pay_held(run_id: uuid.UUID, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
             db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    """Start a NEW run that pays only this run's HELD lines from the employer's own wallet.

    The new run is linked to this one (``parent_run_id``), priced and approved like any run (it waits
    for a TAP-signed human decision), and never touches the contractors this run already paid.
    """
    parent = load_run(db, tenant, run_id)  # org-scoped: another organization's run is a 404
    try:
        run = runtime.start_continuation(db, org=tenant.organization, user=tenant.user, run=parent)
    except RuntimeConflict as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except HeldLineUnavailable as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    db.expire_all()
    return run_detail_out(db, runtime, load_run(db, tenant, run.id))


@router.get("/{run_id}", response_model=RunDetailOut)
def get_run(run_id: uuid.UUID, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
            runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    return run_detail_out(db, runtime, load_run(db, tenant, run_id))


@router.post("/{run_id}/decision", response_model=RunDetailOut)
def decide(request: Request, run_id: uuid.UUID, body: DecisionIn, tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
           db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    run = load_run(db, tenant, run_id)
    try:
        runtime.decide(db, run=run, employer=run.employer, user=tenant.user, decision=body.decision)
    except RuntimeConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    # The REAL human decision (a Visa TAP-signed approval) is what resumes a workflow gate parked on this run.
    hooks.resolve_gate(request, db, tenant, "run", run.id, "APPROVED" if body.decision == "approve" else "REJECTED",
                       tenant.user)
    db.expire_all()
    return run_detail_out(db, runtime, load_run(db, tenant, run_id))


@router.post("/{run_id}/retry", response_model=RunDetailOut)
def retry(run_id: uuid.UUID, tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
          db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> RunDetailOut:
    """Resume a FAILED run from its checkpoint. Completed payouts are skipped and every
    money call reuses its request_id, so nobody is paid twice."""
    run = load_run(db, tenant, run_id)
    if run.status != "FAILED":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a failed run can be retried.")
    try:
        runtime.resume_failed(db, run=run, employer=run.employer)
    except (RuntimeConflict, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.expire_all()
    return run_detail_out(db, runtime, load_run(db, tenant, run_id))
