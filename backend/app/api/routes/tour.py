"""Product tour state + the "Get started" checklist.

``/me/onboarding`` is per user *and* organization: the row is keyed by the session user and
organization, so one member can never read or change another member's progress, and there is no
id in the URL. The checklist is derived from real organization data on every call (no stored
ticks, no fake progress).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import (
    Contractor, Employer, Invitation, LedgerTransaction, Membership, OnboardingState, PayrollRun, Workflow,
)
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_settings

router = APIRouter(prefix="/me/onboarding", tags=["onboarding guide"])

# Bump after a major product change: finished/skipped members are then *offered* the tour again
# (a subtle pill), never forced into it.
TOUR_VERSION = 1

MONEY_MOVED = ("SETTLED", "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING")
CHAPTERS = ("welcome", "setup", "payroll", "safety", "extras", "automation", "trust", "team")


class OnboardingPatch(BaseModel):
    status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "SKIPPED"] | None = None
    last_step: str | None = Field(default=None, max_length=64)
    completed_chapters: list[str] | None = Field(default=None, max_length=24)
    checklist_dismissed: bool | None = None


def _first_name(full_name: str) -> str:
    return (full_name.strip().split() or [""])[0]


def _state_out(tenant: Tenant, row: OnboardingState | None) -> dict:
    demo = bool(tenant.organization.is_demo)
    # No row yet = first session in this org. The demo account never auto-starts (it shows a pill).
    status = row.status if row else ("SKIPPED" if demo else "NOT_STARTED")
    version = row.tour_version if row else TOUR_VERSION
    return {
        "status": status,
        "tour_version": version,
        "current_tour_version": TOUR_VERSION,
        "last_step": row.last_step if row else None,
        "completed_chapters": list(row.completed_chapters or []) if row else [],
        "checklist_dismissed": bool(row.checklist_dismissed) if row else False,
        "auto_start": status == "NOT_STARTED" and not demo,
        "outdated": status in ("COMPLETED", "SKIPPED") and version < TOUR_VERSION,
        "role": tenant.role,
        "is_demo": demo,
        "first_name": _first_name(tenant.user.full_name),
        "organization_name": tenant.organization.name,
    }


def _row(db: Session, tenant: Tenant) -> OnboardingState | None:
    return db.scalar(select(OnboardingState).where(OnboardingState.organization_id == tenant.organization.id,
                                                   OnboardingState.user_id == tenant.user.id))


@router.get("")
def get_state(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> dict:
    return _state_out(tenant, _row(db, tenant))


@router.put("")
def put_state(body: OnboardingPatch, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> dict:
    row = _row(db, tenant)
    if row is None:
        row = OnboardingState(organization_id=tenant.organization.id, user_id=tenant.user.id,
                              tour_version=TOUR_VERSION, status="NOT_STARTED")
        db.add(row)
    if body.status is not None:
        row.status = body.status
        if body.status != "NOT_STARTED":
            row.tour_version = TOUR_VERSION  # the version this member actually saw
    if body.last_step is not None:
        row.last_step = body.last_step
    if body.completed_chapters is not None:
        row.completed_chapters = sorted({c for c in body.completed_chapters
                                         if c in CHAPTERS or (c.startswith("page:") and len(c) <= 40)})
    if body.checklist_dismissed is not None:
        row.checklist_dismissed = body.checklist_dismissed
    db.commit()
    db.refresh(row)
    return _state_out(tenant, row)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


@router.get("/checklist")
def checklist(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)) -> dict:
    org = tenant.organization.id
    state = _row(db, tenant)
    can_setup = tenant.role in EDITOR_ROLES
    employers = _count(db, select(func.count()).select_from(Employer).where(Employer.organization_id == org))
    contractors = _count(db, select(func.count()).select_from(Contractor).where(
        Contractor.organization_id == org, Contractor.is_active.is_(True)))
    funded = _count(db, select(func.count()).select_from(LedgerTransaction).where(
        LedgerTransaction.organization_id == org, LedgerTransaction.memo.like("deposit%")))
    paid = _count(db, select(func.count()).select_from(PayrollRun).where(
        PayrollRun.organization_id == org, PayrollRun.status.in_(MONEY_MOVED)))
    approvers = _count(db, select(func.count()).select_from(Membership).where(
        Membership.organization_id == org, Membership.role.in_(("admin", "approver")))) + _count(
        db, select(func.count()).select_from(Invitation).where(
            Invitation.organization_id == org, Invitation.status == "PENDING",
            Invitation.role.in_(("admin", "approver"))))
    scheduled = _count(db, select(func.count()).select_from(Employer).where(
        Employer.organization_id == org, Employer.next_pay_date.is_not(None))) + _count(
        db, select(func.count()).select_from(Workflow).where(
            Workflow.organization_id == org, Workflow.enabled.is_(True),
            Workflow.trigger_type == "trigger.schedule"))
    items = [
        {"key": "employer", "label": "Add an employer", "hint": "Each client gets its own wallet.",
         "href": "/dashboard/employers/new", "done": employers > 0, "optional": False},
        {"key": "contractors", "label": "Add contractors", "hint": "One by one, or import a CSV.",
         "href": "/dashboard/imports", "done": contractors > 0, "optional": False},
        {"key": "fund", "label": "Fund the wallet", "hint": "The money stays in that client's silo.",
         "href": "/dashboard/funding", "done": funded > 0, "optional": False},
        {"key": "payroll", "label": "Run your first payroll", "hint": "Silo prepares it, you approve it.",
         "href": "/dashboard/execution", "done": paid > 0, "optional": False},
        {"key": "approver", "label": "Invite an approver", "hint": "A second pair of eyes.",
         "href": "/dashboard/team", "done": approvers > 0, "optional": False},
        {"key": "email", "label": "Connect email", "hint": "So reminders and receipts really send.",
         "href": "/dashboard/notifications", "done": settings.email_configured, "optional": True},
        {"key": "schedule", "label": "Set a payroll schedule", "hint": "Pay dates and fund-by dates.",
         "href": "/dashboard/funding", "done": scheduled > 0, "optional": True},
    ]
    required = [i for i in items if not i["optional"]]
    done = sum(1 for i in required if i["done"])
    return {
        "visible": can_setup,
        "dismissed": bool(state.checklist_dismissed) if state else False,
        "items": items if can_setup else [],
        "done_count": done,
        "total": len(required),
        "all_done": done == len(required),
    }
