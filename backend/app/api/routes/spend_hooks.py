"""Spend wiring: module routes -> workflow engine + the platform notifications outbox.

Glue between the VIC spend modules (Onboarding Kit / Travel), the deterministic workflow engine and
:mod:`app.services.notify`. Every helper is best-effort and guarded: a workflow or mail failure is logged and
rolled back, and NEVER breaks the money path that triggered it (the same discipline as the autopilot hooks).
"""

from __future__ import annotations

import logging

from fastapi import Request

from ...services import notify, spend_mail
from ..deps import Tenant

logger = logging.getLogger(__name__)


def dispatch(request: Request, db, tenant: Tenant, event: str, kind: str, entity_id, facts: dict) -> None:
    """Fire a domain event into the workflow engine (guarded)."""
    try:
        from ...services.workflows import dispatch_event

        dispatch_event(db, organization_id=tenant.organization.id, event_type=event, entity_kind=kind,
                       entity_id=str(entity_id), facts=facts, runtime=request.app.state.runtime)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, request.app.state.settings)
    except Exception:  # noqa: BLE001
        logger.exception("workflow dispatch %s for %s %s failed (ignored)", event, kind, entity_id)
        db.rollback()


def resolve_gate(request: Request, db, tenant: Tenant, kind: str, entity_id, decision: str, decided_by) -> None:
    """Resume workflow runs parked at a human gate on this entity. Called ONLY from the real decision routes."""
    try:
        from ...services.workflows import resolve_approval_for_entity

        resolve_approval_for_entity(db, organization_id=tenant.organization.id, entity_kind=kind,
                                    entity_id=str(entity_id), decision=decision, decided_by=decided_by,
                                    runtime=request.app.state.runtime)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, request.app.state.settings)
    except Exception:  # noqa: BLE001
        logger.exception("workflow gate resolution for %s %s failed (ignored)", kind, entity_id)
        db.rollback()


def notify_deciders(request: Request, db, tenant: Tenant, *, context: dict, entity_key: str, employer_id) -> None:
    """Queue an approval request mail for every decider (guarded; delivery is asynchronous)."""
    try:
        context = {**context, "link_url": f"{request.app.state.settings.app_base_url.rstrip('/')}"
                                          f"{context.get('deep_link', '/dashboard')}"}
        spend_mail.notify_deciders(db, tenant.organization, context=context, entity_key=entity_key,
                                   employer_id=employer_id)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, request.app.state.settings)
    except Exception:  # noqa: BLE001
        logger.exception("approval mail for %s failed (ignored)", entity_key)
        db.rollback()


def queue_itinerary(request: Request, db, tenant: Tenant, template: str, to: str, context: dict, *,
                    dedupe_key: str, employer_id, contractor_id=None) -> None:
    """Queue one contractor-facing mail (guarded). ``dedupe_key`` makes a repeat click a no-op."""
    try:
        spend_mail.queue_template(db, tenant.organization, template, to, context, dedupe_key=dedupe_key,
                                  employer_id=employer_id, contractor_id=contractor_id)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, request.app.state.settings)
    except Exception:  # noqa: BLE001
        logger.exception("%s mail failed (ignored)", template)
        db.rollback()
