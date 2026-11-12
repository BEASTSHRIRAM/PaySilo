"""Email for the spend categories and workflows: one entry point onto main's notifications outbox.

Everything is a :class:`~app.db.models.Notification` row queued through :func:`notify.queue_email` (the same
outbox, statuses, retries and SENSITIVE redaction as every other mail). Delivery is asynchronous and never
inline; with no mail credentials the rows stay QUEUED and the notifications page says so honestly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Membership, Notification, Organization, User
from . import email_templates as tpl
from . import notify

DECIDER_ROLES = ("owner", "admin", "approver")
TEMPLATE_NAMES = tuple(tpl.SPEND_TEMPLATES)


def decider_emails(db: Session, organization_id: uuid.UUID) -> list[str]:
    """Emails of the organization's active deciders (owner/admin/approver)."""
    rows = db.execute(
        select(User.email).join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == organization_id, Membership.role.in_(DECIDER_ROLES),
               User.is_active.is_(True))).scalars().all()
    return sorted({e for e in rows if e})


def queue_template(db: Session, org: Organization, template: str, to: str, context: dict, *,
                   dedupe_key: str | None = None, employer_id: uuid.UUID | None = None,
                   contractor_id: uuid.UUID | None = None, meta: dict | None = None) -> Notification:
    """Render one of the spend/workflow templates and queue it. Idempotent on ``dedupe_key``."""
    entry = tpl.SPEND_TEMPLATES.get(template)
    if entry is None:
        raise ValueError(f"Unknown email template {template!r}. Known: {', '.join(TEMPLATE_NAMES)}.")
    builder, kind, sensitive = entry
    subject, text, html = builder(context, org.name)
    return notify.queue_email(db, org, kind, to, subject, text, body_html=html, employer_id=employer_id,
                              contractor_id=contractor_id, dedupe_key=dedupe_key,
                              meta={"template": template, **(meta or {})}, sensitive=sensitive)


def notify_deciders(db: Session, org: Organization, *, context: dict, entity_key: str,
                    employer_id: uuid.UUID | None = None) -> list[Notification]:
    """Queue an approval request for every decider (one per person and entity)."""
    return [queue_template(db, org, "approval_requested", address, context,
                           dedupe_key=f"spendapproval:{entity_key}:{address}", employer_id=employer_id,
                           meta={"entity": entity_key})
            for address in decider_emails(db, org.id)]
