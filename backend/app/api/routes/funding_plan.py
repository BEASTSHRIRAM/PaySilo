"""Funding Planner API: plans, schedules, statements, reminders. Tenant-scoped; no route here moves money."""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ...config import Settings
from ...crew import funding
from ...services import notify, reminders
from ...services.runtime import PlatformRuntime
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from .employers import load_employer
from .notifications import notification_out

router = APIRouter(tags=["funding"])

PAY_FREQUENCIES = ("WEEKLY", "BIWEEKLY", "MONTHLY")


class ScheduleIn(BaseModel):
    """Any field left out is unchanged; ``pay_frequency`` / ``next_pay_date`` may be null to clear them."""

    pay_frequency: Literal["WEEKLY", "BIWEEKLY", "MONTHLY"] | None = None
    next_pay_date: date | None = None
    funding_lead_days: int | None = Field(default=None, ge=0, le=10)

    @field_validator("next_pay_date")
    @classmethod
    def _sane_date(cls, v: date | None) -> date | None:
        if v is not None and not (date.today() - timedelta(days=365) <= v <= date.today() + timedelta(days=730)):
            raise ValueError("pay date must be within the last year and the next two years")
        return v


class ReminderIn(BaseModel):
    kind: Literal["reminder", "statement"] = "reminder"


def _plan(db: Session, runtime: PlatformRuntime, employer) -> dict:
    return funding.plan_employer(db, runtime, employer).as_dict()


@router.get("/funding/plan")
def organization_plan(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                      runtime: PlatformRuntime = Depends(get_runtime)) -> dict:
    return funding.plan_organization(db, runtime, tenant.organization.id)


@router.get("/employers/{employer_id}/funding-plan")
def employer_plan(employer_id: uuid.UUID, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                  runtime: PlatformRuntime = Depends(get_runtime)) -> dict:
    return _plan(db, runtime, load_employer(db, tenant, employer_id))


@router.patch("/employers/{employer_id}/schedule")
def update_schedule(employer_id: uuid.UUID, body: ScheduleIn,
                    tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db),
                    runtime: PlatformRuntime = Depends(get_runtime)) -> dict:
    emp = load_employer(db, tenant, employer_id)
    given = body.model_fields_set
    if "pay_frequency" in given:
        emp.pay_frequency = body.pay_frequency
    if "next_pay_date" in given:
        emp.next_pay_date = body.next_pay_date
    if "funding_lead_days" in given and body.funding_lead_days is not None:
        emp.funding_lead_days = body.funding_lead_days
    db.commit()
    return _plan(db, runtime, emp)


@router.get("/employers/{employer_id}/funding-statement")
def funding_statement(employer_id: uuid.UUID, format: Literal["json", "csv"] = "json",
                      tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                      runtime: PlatformRuntime = Depends(get_runtime)):
    emp = load_employer(db, tenant, employer_id)
    st = funding.statement(_plan(db, runtime, emp), tenant.organization.name)
    if format == "json":
        return st
    slug = re.sub(r"[^a-z0-9]+", "-", emp.name.lower()).strip("-") or "employer"
    return Response(funding.statement_csv(st), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="funding-statement-{slug}-{st["generated_on"]}.csv"'})


@router.post("/employers/{employer_id}/funding-reminder", status_code=status.HTTP_201_CREATED)
def send_reminder(request: Request, employer_id: uuid.UUID, body: ReminderIn | None = None,
                  tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db),
                  runtime: PlatformRuntime = Depends(get_runtime), settings: Settings = Depends(get_settings)) -> dict:
    body = body or ReminderIn()
    emp = load_employer(db, tenant, employer_id)
    if not emp.contact_email:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{emp.name} has no contact email yet. Add one before sending a funding email.")
    plan = _plan(db, runtime, emp)
    if plan["status"] == "RATE_UNAVAILABLE":
        raise HTTPException(status.HTTP_409_CONFLICT, plan["message"])
    if plan["status"] == "NO_LINES":
        raise HTTPException(status.HTTP_409_CONFLICT, "There is nothing to fund yet: no contractor has a default amount.")
    if body.kind == "statement":
        kind = "FUNDING_STATEMENT"
    elif not plan["is_short"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "This wallet already covers the payroll, so no reminder is needed.")
    else:
        kind = "FUNDING_OVERDUE" if plan["status"] == "OVERDUE" else "FUNDING_REMINDER"
    row = reminders.send_funding_email(db, tenant.organization, emp, plan, kind, settings,
                                       meta={"source": "manual", "requested_by": str(tenant.user.id)})
    db.commit()
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)  # delivery never blocks this request
    return notification_out(row, emp.name)


@router.post("/funding/reminders/run")
def run_sweep(request: Request, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db),
              runtime: PlatformRuntime = Depends(get_runtime), settings: Settings = Depends(get_settings)) -> dict:
    results = reminders.sweep_organization(db, runtime, settings, tenant.organization)
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return {"queued": sum(1 for r in results if r["action"] == "queued"),
            "skipped": sum(1 for r in results if r["action"] == "skipped"), "results": results}
