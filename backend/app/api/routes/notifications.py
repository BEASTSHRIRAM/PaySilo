"""Notifications outbox (read-only listing). Every row is scoped to the caller's organization."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import NOTIFICATION_KINDS, NOTIFICATION_STATUSES, Employer, Notification
from ...config import Settings
from ...services.notify import email_status
from ..deps import Tenant, current_tenant, get_db, get_settings

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
