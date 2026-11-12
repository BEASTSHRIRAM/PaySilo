"""Spend Workflows — saved deterministic node graphs, their runs and tasks.

Org scoping mirrors benefits/runs: every query filters by the caller's
organization and another organization's rows are a 404. Reads are open
to any org member; writes require EDITOR_ROLES (owner/admin), matching
how benefits treats budget writes (EDITOR) — decisions themselves never
happen here: there is deliberately no approve/purchase/pay endpoint.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...db.models import Workflow, WorkflowRun, WorkflowTask
from ...services.workflows import build_payroll_brief, run_due_schedules, trigger_type_of, validate_graph
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, require_roles
from ..schemas import (
    WorkflowBriefEntryOut,
    WorkflowIn,
    WorkflowOut,
    WorkflowRunOut,
    WorkflowTaskOut,
    WorkflowUpdateIn,
)
from ..serializers import workflow_out, workflow_run_out, workflow_task_out

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _load(db: Session, tenant: Tenant, workflow_id: uuid.UUID) -> Workflow:
    wf = db.scalar(select(Workflow).where(Workflow.id == workflow_id,
                                          Workflow.organization_id == tenant.organization.id))
    if wf is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found.")
    return wf


def _validate_or_422(graph) -> None:
    errors = validate_graph(graph)
    if errors:
        raise HTTPException(422, detail={"code": "INVALID_GRAPH", "message": "; ".join(errors),
                                         "errors": errors})


@router.get("", response_model=list[WorkflowOut])
@router.get("/", response_model=list[WorkflowOut])
def list_workflows(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[WorkflowOut]:
    rows = db.scalars(select(Workflow).where(Workflow.organization_id == tenant.organization.id)
                      .order_by(Workflow.created_at)).all()
    return [workflow_out(w) for w in rows]


@router.post("", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                    db: Session = Depends(get_db)) -> WorkflowOut:
    _validate_or_422(body.graph)
    wf = Workflow(organization_id=tenant.organization.id, name=body.name, description=body.description,
                  enabled=body.enabled, trigger_type=trigger_type_of(body.graph) or "",
                  graph=body.graph, created_by_id=tenant.user.id)
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return workflow_out(wf)


@router.get("/runs", response_model=list[WorkflowRunOut])
def list_runs(workflow_id: uuid.UUID | None = None, status_filter: str | None = Query(None, alias="status"),
              tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[WorkflowRunOut]:
    q = (select(WorkflowRun).where(WorkflowRun.organization_id == tenant.organization.id)
         .order_by(WorkflowRun.created_at.desc()).limit(200))
    if workflow_id:
        q = q.where(WorkflowRun.workflow_id == workflow_id)
    if status_filter:
        q = q.where(WorkflowRun.status == status_filter)
    return [workflow_run_out(r, db) for r in db.scalars(q)]


@router.get("/runs/{run_id}", response_model=WorkflowRunOut)
def get_run(run_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
            db: Session = Depends(get_db)) -> WorkflowRunOut:
    run = db.scalar(select(WorkflowRun).options(selectinload(WorkflowRun.steps))
                    .where(WorkflowRun.id == run_id,
                           WorkflowRun.organization_id == tenant.organization.id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow run not found.")
    return workflow_run_out(run, db, include_steps=True)


@router.get("/tasks", response_model=list[WorkflowTaskOut])
def list_tasks(status_filter: str | None = Query(None, alias="status"),
               tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[WorkflowTaskOut]:
    q = (select(WorkflowTask).where(WorkflowTask.organization_id == tenant.organization.id)
         .order_by(WorkflowTask.created_at.desc()).limit(200))
    if status_filter:
        q = q.where(WorkflowTask.status == status_filter)
    return [workflow_task_out(t) for t in db.scalars(q)]


@router.get("/brief", response_model=list[WorkflowBriefEntryOut])
def payroll_morning_brief(tenant: Tenant = Depends(current_tenant),
                          db: Session = Depends(get_db)) -> list[WorkflowBriefEntryOut]:
    """Payroll Morning Brief: one entry per WAITING run gated on a payroll run."""
    return [WorkflowBriefEntryOut(**entry) for entry in build_payroll_brief(db, organization_id=tenant.organization.id)]


@router.post("/schedules/tick")
def schedules_tick(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                   db: Session = Depends(get_db)) -> dict:
    """Fire due schedule workflows at server-now. Production scheduling is
    an external cron hitting this endpoint — there is no daemon."""
    from datetime import UTC, datetime

    runs = run_due_schedules(db, now=datetime.now(UTC), settings=None)
    db.commit()
    return {"fired": len(runs),
            "runs": [workflow_run_out(r, db).model_dump(mode="json") for r in runs]}


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                 db: Session = Depends(get_db)) -> WorkflowOut:
    return workflow_out(_load(db, tenant, workflow_id))


@router.put("/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: uuid.UUID, body: WorkflowUpdateIn,
                    tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                    db: Session = Depends(get_db)) -> WorkflowOut:
    wf = _load(db, tenant, workflow_id)
    if body.graph is not None:
        _validate_or_422(body.graph)
        wf.graph = body.graph
        wf.trigger_type = trigger_type_of(body.graph) or ""
    if body.name is not None:
        wf.name = body.name
    if body.description is not None:
        wf.description = body.description
    if body.enabled is not None:
        wf.enabled = body.enabled
    db.commit()
    db.refresh(wf)
    return workflow_out(wf)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: uuid.UUID, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                    db: Session = Depends(get_db)) -> None:
    wf = _load(db, tenant, workflow_id)
    db.delete(wf)
    db.commit()
