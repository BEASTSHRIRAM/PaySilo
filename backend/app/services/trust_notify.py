"""Contractor-trust emails through the Notification outbox (delivered by the Brevo SMTP sweep).

* ``contractor_onboarding_link``: to the contractor. The body carries the one-time link, so the row is
  SENSITIVE (see :func:`notify.queue_email`): the link is kept only until delivery and is never shown by
  the notifications list/API.
* ``bank_change_callback_request``: to the employer contact and the approvers. Masked account only (last 4),
  never full bank details.

Never raises: a mail problem must not undo the change that triggered it.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Contractor, Employer, Organization
from . import autopilot, notify
from . import email_templates as tpl
from .contractor_trust import mask

logger = logging.getLogger("paysilo.trust_notify")


def onboarding_link(db: Session, settings: Settings, org: Organization, employer: Employer, contractor: Contractor,
                    *, invite_id, token: str, days: int) -> bool:
    """Queue the onboarding email. Returns False when the contractor has no email address."""
    if not contractor.email:
        return False
    try:
        url = f"{settings.app_base_url.rstrip('/')}/onboard/{token}"
        subject, text, html = tpl.TEMPLATES["onboarding_link"].build(
            {"contractor": contractor.full_name, "employer": employer.name, "url": url, "days": days}, org.name)
        notify.queue_email(db, org, "ONBOARDING_LINK", contractor.email, subject, text, body_html=html,
                           employer_id=employer.id, contractor_id=contractor.id, dedupe_key=f"onboard:{invite_id}",
                           meta={"invite_id": str(invite_id)}, sensitive=True)
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Queueing the onboarding email failed.")
        db.rollback()
        return False


def bank_change_callback_request(db: Session, settings: Settings, org: Organization, employer: Employer,
                                 contractor: Contractor) -> int:
    """Ask the employer contact and the approvers to confirm a bank change by callback. Returns new rows."""
    try:
        masked = mask(contractor.iban or contractor.account_number)
        url = f"{settings.app_base_url.rstrip('/')}/dashboard/employers/{employer.id}"
        changed = contractor.bank_details_changed_at.isoformat() if contractor.bank_details_changed_at else "x"
        recipients = [u.email for u in autopilot.approvers(db, org)]
        if employer.contact_email:
            recipients.append(employer.contact_email)
        seen: set[str] = set()
        new = 0
        for to in recipients:
            if to.lower() in seen:
                continue
            seen.add(to.lower())
            subject, text, html = tpl.TEMPLATES["bank_change_callback"].build(
                {"contractor": contractor.full_name, "employer": employer.name, "masked": masked, "url": url}, org.name)
            key = f"bankcb:{contractor.id}:{changed}:{to.lower()}"
            before = db.query(notify.Notification.id).filter_by(organization_id=org.id, dedupe_key=key).first()
            notify.queue_email(db, org, "BANK_CHANGE_CALLBACK", to, subject, text, body_html=html,
                               employer_id=employer.id, contractor_id=contractor.id, dedupe_key=key)
            new += before is None
        db.commit()
        return new
    except Exception:  # noqa: BLE001
        logger.exception("Queueing the bank-change callback request failed.")
        db.rollback()
        return 0


def onboarding_submitted(db: Session, settings: Settings, org: Organization, employer: Employer,
                         contractor: Contractor) -> int:
    """Tell the staff (owners, admins, approvers) a contractor submitted details. One per contractor submission."""
    try:
        url = f"{settings.app_base_url.rstrip('/')}/dashboard/employers/{employer.id}"
        stamp = contractor.onboarded_at.isoformat() if contractor.onboarded_at else "x"
        new = 0
        for user in autopilot.approvers(db, org):
            subject, text, html = tpl.TEMPLATES["onboarding_submitted"].build(
                {"name": (user.full_name or "").split(" ")[0] or None, "contractor_name": contractor.full_name,
                 "employer_name": employer.name, "url": url}, org.name)
            key = f"onbsub:{contractor.id}:{stamp}:{user.id}"
            before = db.query(notify.Notification.id).filter_by(organization_id=org.id, dedupe_key=key).first()
            notify.queue_email(db, org, "ONBOARDING_SUBMITTED", user.email, subject, text, body_html=html,
                               employer_id=employer.id, contractor_id=contractor.id, dedupe_key=key)
            new += before is None
        db.commit()
        return new
    except Exception:  # noqa: BLE001
        logger.exception("Queueing the onboarding submitted mail failed.")
        db.rollback()
        return 0


def bank_verified(db: Session, settings: Settings, org: Organization, employer: Employer,
                  contractor: Contractor) -> bool:
    """Tell the contractor their bank details were verified (last 4 only)."""
    if not contractor.email:
        return False
    try:
        digits = "".join(ch for ch in (contractor.iban or contractor.account_number or "") if ch.isalnum())
        verified = contractor.bank_verified_at.isoformat() if contractor.bank_verified_at else "x"
        subject, text, html = tpl.TEMPLATES["bank_verified"].build(
            {"contractor_name": contractor.full_name, "employer_name": employer.name, "last4": digits[-4:]}, org.name)
        notify.queue_email(db, org, "BANK_VERIFIED", contractor.email, subject, text, body_html=html,
                           employer_id=employer.id, contractor_id=contractor.id,
                           dedupe_key=f"bankok:{contractor.id}:{verified}")
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Queueing the bank verified mail failed.")
        db.rollback()
        return False
