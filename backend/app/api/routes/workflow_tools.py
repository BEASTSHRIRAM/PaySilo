"""Workflow builder helpers: node registry, graph validation (no side effects), schedule preview, calendar
occurrences and the payroll-schedule wizard. Registered BEFORE the main workflows router so these static paths win
over ``/workflows/{workflow_id}``.

All reads/writes are scoped to the caller's organisation. Nothing here approves, runs or pays.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import Employer, Workflow
from ...services import workflow_schedule as sched
from ...services.workflow_registry import node_registry
from ...services.workflow_templates import build_payroll_schedule_graph, layout_graph
from ...services.workflow_validation import MAX_GRAPH_BYTES, validate_graph_detailed
from ...services.workflows import trigger_type_of
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, require_roles
from ..schemas import WorkflowOut
from ..serializers import workflow_out

router = APIRouter(prefix="/workflows", tags=["workflows"])


def employer_ids_of(db: Session, tenant: Tenant) -> set[str]:
    return {str(i) for i in db.scalars(select(Employer.id).where(Employer.organization_id == tenant.organization.id))}


def check_graph(db: Session, tenant: Tenant, graph) -> list[dict]:
    """Detailed validation errors for a graph, tenant-aware (employer scope) with a size limit."""
    try:
        too_big = len(json.dumps(graph)) > MAX_GRAPH_BYTES
    except (TypeError, ValueError):
        return [{"node_id": None, "edge_id": None, "field": None, "code": "shape",
                 "message": "Graph is not valid JSON."}]
    if too_big:
        return [{"node_id": None, "edge_id": None, "field": None, "code": "size",
                 "message": f"Graph is larger than {MAX_GRAPH_BYTES // 1000} KB."}]
    return validate_graph_detailed(graph, employer_ids=employer_ids_of(db, tenant))


class ValidateIn(BaseModel):
    graph: dict | list | str | int | None = None


class SchedulePreviewIn(BaseModel):
    config: dict
    count: int = Field(default=3, ge=1, le=12)


class PayrollScheduleIn(BaseModel):
    employer_id: uuid.UUID
    day: int | None = Field(default=None, ge=1, le=31)
    last_day: bool = False
    time: str = "09:00"
    timezone: str = sched.DEFAULT_TIMEZONE
    weekend_rule: str = "as_is"
    enabled: bool = False  # the wizard sets this only after the explicit confirm
    name: str | None = Field(default=None, max_length=200)


def schedule_node(graph) -> dict | None:
    return next((n for n in (graph or {}).get("nodes") or []
                 if isinstance(n, dict) and n.get("type") == "trigger.schedule"), None)


def _summary(wf: Workflow) -> str:
    node = schedule_node(wf.graph)
    try:
        return sched.describe(node.get("data") or {}) if node else ""
    except sched.ScheduleConfigError:
        return ""


def payroll_schedules_for(db: Session, organization_id: uuid.UUID, employer_id: str | None = None) -> list[Workflow]:
    """Enabled schedule workflows of this organisation that prepare payroll (optionally for one employer)."""
    out = []
    for wf in db.scalars(select(Workflow).where(Workflow.organization_id == organization_id,
                                                Workflow.enabled.is_(True),
                                                Workflow.trigger_type == "trigger.schedule")):
        types = {n.get("type") for n in (wf.graph or {}).get("nodes") or [] if isinstance(n, dict)}
        node = schedule_node(wf.graph)
        if "action.prepare_payroll_run" not in types or node is None:
            continue
        data = node.get("data") or {}
        if employer_id is None or data.get("employer_id") == employer_id or data.get("fan_out") == "employers":
            out.append(wf)
    return out


@router.get("/node-registry")
def get_node_registry(tenant: Tenant = Depends(current_tenant)) -> dict:
    """Every node type with its config fields, handles and the connection rules (what the validator accepts)."""
    return node_registry()


@router.post("/validate")
def validate_workflow_graph(body: ValidateIn, tenant: Tenant = Depends(current_tenant),
                            db: Session = Depends(get_db)) -> dict:
    """Validate a graph (e.g. imported JSON) against the registry and this organisation. No side effects.

    Also accepts the exported ``{name, description, graph}`` envelope. A valid graph without positions comes back
    laid out left to right in ``graph``.
    """
    graph = body.graph
    if isinstance(graph, dict) and "nodes" not in graph and isinstance(graph.get("graph"), dict):
        graph = graph["graph"]
    detailed = check_graph(db, tenant, graph)
    laid_out = None
    if not detailed and isinstance(graph, dict):
        has_positions = all(isinstance(n.get("position"), dict) for n in graph.get("nodes", []))
        laid_out = graph if has_positions and graph.get("layout") == "lr" else layout_graph(graph)
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    return {"valid": not detailed, "errors": detailed,
            "trigger_type": trigger_type_of(graph) if isinstance(graph, dict) else None,
            "node_count": len(nodes) if isinstance(nodes, list) else 0, "graph": laid_out}


@router.post("/schedule-preview")
def schedule_preview(body: SchedulePreviewIn, tenant: Tenant = Depends(current_tenant)) -> dict:
    """Next runs of a schedule config, computed on the server (the UI never does calendar maths)."""
    try:
        rows = sched.preview(body.config, datetime.now(UTC), body.count)
        return {"valid": True, "summary": sched.describe(body.config), "next_runs": rows,
                "timezone": sched.parse_config(body.config)["tz_name"],
                "note": "Weekends follow the weekend rule; public holidays are not modelled."}
    except sched.ScheduleConfigError as exc:
        return {"valid": False, "field": exc.field, "message": str(exc), "next_runs": []}


@router.get("/schedule-occurrences")
def schedule_occurrences(start: date, end: date, tenant: Tenant = Depends(current_tenant),
                         db: Session = Depends(get_db)) -> list[dict]:
    """Occurrences of this organisation's ENABLED schedule workflows between two dates (read-only; calendar chips)."""
    if end < start or (end - start).days > 400:
        raise HTTPException(422, "Choose a date range of at most 400 days.")
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC) - timedelta(days=1)
    hi = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=2)
    names = {str(e.id): e.name for e in db.scalars(select(Employer).where(
        Employer.organization_id == tenant.organization.id))}
    out = []
    for wf in db.scalars(select(Workflow).where(Workflow.organization_id == tenant.organization.id,
                                                Workflow.enabled.is_(True),
                                                Workflow.trigger_type == "trigger.schedule")):
        node = schedule_node(wf.graph)
        if node is None:
            continue
        data = node.get("data") or {}
        try:
            cfg = sched.parse_config(data)
            moments = sched.occurrences(data, lo, hi)
        except sched.ScheduleConfigError:
            continue
        employer_id = data.get("employer_id")
        for moment in moments:
            local = moment.astimezone(cfg["tz"])
            if not start <= local.date() <= end:
                continue
            out.append({"workflow_id": str(wf.id), "workflow_name": wf.name, "employer_id": employer_id,
                        "employer_name": names.get(str(employer_id)) if employer_id else None,
                        "date": local.date().isoformat(), "time": local.strftime("%H:%M"),
                        "timezone": cfg["tz_name"]})
    return sorted(out, key=lambda r: (r["date"], r["time"]))


@router.get("/employer-schedules")
def list_payroll_schedules(employer_id: uuid.UUID | None = None, tenant: Tenant = Depends(current_tenant),
                           db: Session = Depends(get_db)) -> list[dict]:
    """Enabled payroll schedules (for the wizard's duplicate warning)."""
    return [{"id": str(w.id), "name": w.name, "summary": _summary(w)}
            for w in payroll_schedules_for(db, tenant.organization.id, str(employer_id) if employer_id else None)]


@router.post("/employer-schedules", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_payroll_schedule(body: PayrollScheduleIn, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                            db: Session = Depends(get_db)) -> WorkflowOut:
    """Create the standard 'Monthly Payroll Day' workflow for ONE employer. It is enabled only when the caller
    says so after the wizard's explicit confirm. The workflow prepares the run and asks a human; it never approves."""
    employer = db.scalar(select(Employer).where(Employer.id == body.employer_id,
                                                Employer.organization_id == tenant.organization.id))
    if employer is None:
        raise HTTPException(404, "Employer not found.")
    if body.day is None and not body.last_day:
        raise HTTPException(422, "Choose a day of the month (or 'last day').")
    graph = build_payroll_schedule_graph(employer_id=str(employer.id), employer_name=employer.name, day=body.day,
                                         last_day=body.last_day, time=body.time, timezone=body.timezone,
                                         weekend_rule=body.weekend_rule)
    detailed = check_graph(db, tenant, graph)
    if detailed:
        errors = [e["message"] for e in detailed]
        raise HTTPException(422, detail={"code": "INVALID_GRAPH", "message": "; ".join(errors), "errors": errors,
                                         "details": detailed})
    day_text = "the last day of the month" if body.last_day else f"day {body.day} of the month"
    wf = Workflow(organization_id=tenant.organization.id,
                  name=body.name or f"Monthly Payroll Day: {employer.name}",
                  description=(f"On {day_text} at {body.time} ({body.timezone}), prepare {employer.name}'s payroll "
                               "run and ask the approvers to decide. The workflow never approves or pays."),
                  enabled=body.enabled, trigger_type="trigger.schedule", graph=graph, created_by_id=tenant.user.id)
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return workflow_out(wf)
