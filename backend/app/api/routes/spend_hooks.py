"""Spend wiring: module routes -> workflow engine + email outbox.

Coordinator-owned glue between the VIC spend modules (Benefits /
Onboarding Kit / Travel), the deterministic workflow engine, and the
email outbox. Every helper is best-effort and guarded: a workflow or
email failure is logged and rolled back, and NEVER breaks the money
path that triggered it — the same discipline as the employers/runs
wiring (``_dispatch_workflow`` there, gate resolution in runs.py).
"""

from __future__ import annotations

import logging as _logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import Membership, User
from ..deps import APPROVER_ROLES, Tenant

_wf_logger = _logging.getLogger(__name__)


def dispatch(db: Session, tenant: Tenant, event: str, kind: str, entity_id, facts: dict) -> None:
    """Fire a domain event into the workflow engine (guarded)."""
    try:
        from ...services.workflows import dispatch_event

        dispatch_event(db, organization_id=tenant.organization.id, event_type=event,
                       entity_kind=kind, entity_id=str(entity_id), facts=facts)
        db.commit()
    except Exception:  # noqa: BLE001
        _wf_logger.exception("workflow dispatch %s for %s %s failed (ignored)", event, kind, entity_id)
        db.rollback()


def resolve_gate(db: Session, tenant: Tenant, kind: str, entity_id, decision: str, decided_by) -> None:
    """Resume workflow runs parked at a human gate on this entity (guarded)."""
    try:
        from ...services.workflows import resolve_approval_for_entity

        resolve_approval_for_entity(db, organization_id=tenant.organization.id,
                                    entity_kind=kind, entity_id=str(entity_id),
                                    decision=decision, decided_by=decided_by)
        db.commit()
    except Exception:  # noqa: BLE001
        _wf_logger.exception("workflow gate resolution for %s %s failed (ignored)", kind, entity_id)
        db.rollback()


def decider_emails(db: Session, organization_id) -> list[str]:
    """Emails of the organization's deciders (owner/admin/approver)."""
    rows = db.execute(
        select(User.email).join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == organization_id,
               Membership.role.in_(APPROVER_ROLES))).scalars().all()
    return sorted({e for e in rows if e})


def enqueue(db: Session, settings: Settings, tenant: Tenant, *, template: str, to: str,
            context: dict, related_kind: str, related_id, related_label: str = "") -> None:
    """Record one email in the outbox (guarded; never sends inline)."""
    try:
        from ...services.email import enqueue_email

        enqueue_email(db, settings, organization_id=tenant.organization.id,
                      template=template, to=to, context=context,
                      related_kind=related_kind, related_id=related_id,
                      related_label=related_label)
        db.commit()
    except Exception:  # noqa: BLE001
        _wf_logger.exception("email enqueue %s to %s failed (ignored)", template, to)
        db.rollback()


def notify_deciders(db: Session, settings: Settings, tenant: Tenant, *, context: dict,
                    related_kind: str, related_id, related_label: str = "") -> None:
    """Enqueue an approval_requested email for every decider."""
    for address in decider_emails(db, tenant.organization.id):
        enqueue(db, settings, tenant, template="approval_requested", to=address,
                context=context, related_kind=related_kind, related_id=related_id,
                related_label=related_label)
