"""Platform email — outbox status, listing and drain.

The outbox is recorded first and sent only by an explicit drain (see
``app/services/email.py`` for the full law). Every query is scoped to
the caller's organization exactly like the benefits routes: another
organization's rows are simply not visible.

The status endpoint exposes only whether email is configured and the
From address — never the SMTP user or password.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import EmailOutbox
from ...services.email import drain_outbox, email_configured
from ..deps import EDITOR_ROLES, Tenant, current_tenant, get_db, get_settings, require_roles

router = APIRouter(prefix="/emails", tags=["emails"])


class EmailStatusOut(BaseModel):
    configured: bool
    from_address: str | None


class EmailOutboxOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID | None
    to_address: str
    template: str
    subject: str
    body_text: str
    body_html: str
    status: str
    related_label: str
    related_kind: str
    related_id: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


def _outbox_out(row: EmailOutbox) -> EmailOutboxOut:
    return EmailOutboxOut(
        id=row.id, organization_id=row.organization_id, to_address=row.to_address,
        template=row.template, subject=row.subject, body_text=row.body_text,
        body_html=row.body_html, status=row.status, related_label=row.related_label,
        related_kind=row.related_kind, related_id=row.related_id,
        error_detail=row.error_detail, created_at=row.created_at,
        updated_at=row.updated_at, sent_at=row.sent_at)


@router.get("/status", response_model=EmailStatusOut)
def get_status(tenant: Tenant = Depends(current_tenant),
               settings: Settings = Depends(get_settings)) -> EmailStatusOut:
    return EmailStatusOut(configured=email_configured(settings),
                          from_address=settings.email_from or None)


@router.get("/outbox", response_model=list[EmailOutboxOut])
def list_outbox(tenant: Tenant = Depends(current_tenant),
                db: Session = Depends(get_db)) -> list[EmailOutboxOut]:
    rows = db.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.organization_id == tenant.organization.id)
        .order_by(EmailOutbox.created_at.desc(), EmailOutbox.id.desc())
        .limit(100))
    return [_outbox_out(row) for row in rows]


@router.post("/outbox/drain")
def drain(tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
          db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)) -> dict:
    result = drain_outbox(db, settings)
    db.commit()
    return result
