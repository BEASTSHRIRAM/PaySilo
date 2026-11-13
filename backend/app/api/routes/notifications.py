"""Notifications outbox (read-only listing). Every row is scoped to the caller's organization."""

from __future__ import annotations

import uuid

import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import NOTIFICATION_KINDS, NOTIFICATION_STATUSES, Employer, Notification
from ...config import Settings
from ...services import email_templates as tpl
from ...services import notify
from ...services.notify import email_status
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_settings, require_roles

router = APIRouter(prefix="/notifications", tags=["notifications"])


def notification_out(n: Notification, employer_name: str | None = None) -> dict:
    return {
        "id": str(n.id), "kind": n.kind, "channel": n.channel, "recipient": n.recipient, "subject": n.subject,
        "body": n.body, "sensitive": bool(n.sensitive), "status": n.status, "provider": n.provider,
        "attempts": n.attempts or 0, "error": n.error,
        "last_attempt_at": n.last_attempt_at.isoformat() if n.last_attempt_at else None,
        "employer_id": str(n.employer_id) if n.employer_id else None, "employer_name": employer_name,
        "created_at": n.created_at.isoformat(), "sent_at": n.sent_at.isoformat() if n.sent_at else None,
    }


@router.get("")
def list_notifications(employer_id: uuid.UUID | None = None, kind: str | None = Query(default=None),
                       status: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200),
                       offset: int = Query(default=0, ge=0), tenant: Tenant = Depends(current_tenant),
                       db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    where = [Notification.organization_id == tenant.organization.id]
    if employer_id:
        where.append(Notification.employer_id == employer_id)
    if kind:
        if kind not in NOTIFICATION_KINDS:
            raise HTTPException(422, f"kind must be one of {', '.join(NOTIFICATION_KINDS)}.")
        where.append(Notification.kind == kind)
    if status:
        if status not in NOTIFICATION_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(NOTIFICATION_STATUSES)}.")
        where.append(Notification.status == status)
    total = db.scalar(select(func.count()).select_from(Notification).where(*where)) or 0
    rows = db.execute(
        select(Notification, Employer.name).outerjoin(Employer, Employer.id == Notification.employer_id)
        .where(*where).order_by(Notification.created_at.desc(), Notification.id).limit(limit).offset(offset)).all()
    return {"email": email_status(settings), "total": total, "limit": limit, "offset": offset, "items": [notification_out(n, name) for n, name in rows]}


# ------------------------------------------------------------------ template preview + test send
TEST_LIMIT, TEST_WINDOW = 5, 3600.0
_test_sends: dict[str, deque] = {}
_test_lock = threading.Lock()


def _template_out(spec: tpl.Spec, org_name: str) -> dict:
    subject, text, html = spec.build(dict(spec.sample), org_name)
    return {"key": spec.key, "label": spec.label, "group": spec.group, "kind": spec.kind, "sensitive": spec.sensitive,
            "recipient": spec.recipient, "when": spec.when, "subject": subject, "text": text, "html": html}


@router.get("/templates")
def list_templates(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                   settings: Settings = Depends(get_settings)) -> dict:
    """Every email PaySilo can send, rendered with sample data (owner and admin only)."""
    return {"email": email_status(settings), "can_send_test": settings.email_configured,
            "items": [_template_out(spec, tenant.organization.name) for spec in tpl.TEMPLATES.values()]}


@router.post("/templates/{key}/test")
def send_test(key: str, request: Request, tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
              db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    """Send one template with sample data to the signed in user's own address and nobody else."""
    spec = tpl.TEMPLATES.get(key)
    if spec is None:
        raise HTTPException(404, "Template not found.")
    if not settings.email_configured:
        raise HTTPException(409, {"code": "EMAIL_NOT_CONFIGURED",
                                  "message": "Email delivery is not set up. Add the Brevo SMTP settings first."})
    with _test_lock:
        q = _test_sends.setdefault(str(tenant.user.id), deque())
        now = time.monotonic()
        while q and now - q[0] > TEST_WINDOW:
            q.popleft()
        if len(q) >= TEST_LIMIT:
            raise HTTPException(429, {"code": "RATE_LIMITED", "message": "Too many test emails. Try again later."})
        q.append(now)
    subject, text, html = spec.build(dict(spec.sample), tenant.organization.name)
    notify.queue_email(db, tenant.organization, spec.kind, tenant.user.email, f"Test: {subject}"[:300], text,
                       body_html=html, meta={"template": spec.key, "test": True}, sensitive=spec.sensitive)
    db.commit()
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return {"queued": True, "recipient": tenant.user.email}
