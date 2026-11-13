"""Weekly tax form alert for staff, from the same data as the compliance overview (no second source of truth)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Membership, Organization, User
from . import notify
from . import email_templates as tpl

STAFF_ROLES = ("owner", "admin")


def tax_form_alert(db: Session, settings: Settings, org: Organization, today: date | None = None) -> int:
    """One mail per owner or admin per ISO week when any contractor tax form is missing, expired or expiring."""
    from ..api.routes.contractor_trust import _compliance  # lazy: routes import services

    today = today or date.today()
    overview = _compliance(db, SimpleNamespace(organization=org), settings)
    forms = [f for f in overview["tax_forms"] if f["issue"] != "status_unknown"]
    if not forms:
        return 0
    items = [{"contractor_name": f["full_name"], "employer_name": f["employer_name"], "issue": f["issue"],
              "expires": f["tax_form_expires_at"]} for f in forms]
    year, week, _ = today.isocalendar()
    users = db.scalars(select(User).join(Membership, Membership.user_id == User.id)
                       .where(Membership.organization_id == org.id, Membership.role.in_(STAFF_ROLES),
                              User.is_active.is_(True)).order_by(User.email)).all()
    new = 0
    for user in users:
        key = f"taxform:{year}W{week}:{user.id}"
        if db.scalar(select(notify.Notification.id).where(notify.Notification.organization_id == org.id,
                                                          notify.Notification.dedupe_key == key)):
            continue
        ctx = {"name": (user.full_name or "").split(" ")[0] or None, "items": items, "count": len(forms),
               "url": f"{settings.app_base_url.rstrip('/')}/dashboard/compliance"}
        subject, text, html = tpl.TEMPLATES["tax_form_alert"].build(ctx, org.name)
        notify.queue_email(db, org, "TAX_FORM_ALERT", user.email, subject, text, body_html=html, dedupe_key=key,
                           meta={"count": len(forms)})
        new += 1
    return new
