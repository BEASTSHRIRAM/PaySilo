"""Notifications outbox: queue a message, then deliver it through the configured provider.

``EMAIL_PROVIDER=console`` (default) marks the message SENT with ``provider='console'`` and sends nothing:
the UI labels it "simulated delivery". ``smtp`` really sends through ``SMTP_*``. A delivery failure never
raises to the caller: the row becomes FAILED with a short, safe error text (no credentials, no traceback).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import NOTIFICATION_KINDS, Notification, Organization

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """A provider could not deliver. The message must already be safe to store and show."""


class EmailProvider(Protocol):
    name: str

    def send(self, *, recipient: str, subject: str, body: str) -> None: ...


class ConsoleProvider:
    """Simulated delivery: nothing leaves the machine."""

    name = "console"

    def send(self, *, recipient: str, subject: str, body: str) -> None:
        logger.info("Simulated email queued for delivery (subject length %d).", len(subject))


class SmtpProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, sender: str,
                 smtp_factory=None):
        self.host, self.port, self.user, self.password, self.sender = host, port, user, password, sender
        self._factory = smtp_factory

    def send(self, *, recipient: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, recipient, subject
        msg.set_content(body)
        try:
            smtp = self._factory(self.host, self.port, timeout=15) if self._factory else \
                smtplib.SMTP(self.host, self.port, timeout=15)
            with smtp:
                if self.port != 25:
                    smtp.starttls(context=ssl.create_default_context())
                if self.user and self.password:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise ProviderError("The mail server rejected the SMTP sign-in.") from None
        except smtplib.SMTPRecipientsRefused:
            raise ProviderError("The mail server refused the recipient address.") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise ProviderError(f"The mail server could not be reached or refused the message ({type(exc).__name__}).") from None


def provider_for(settings: Settings) -> EmailProvider:
    if settings.email_provider == "smtp":
        if not (settings.smtp_host and settings.smtp_from):
            raise ProviderError("SMTP is selected but SMTP_HOST / SMTP_FROM are not configured.")
        return SmtpProvider(settings.smtp_host, settings.smtp_port, settings.smtp_user,
                            settings.smtp_password, settings.smtp_from)
    return ConsoleProvider()


def queue_email(db: Session, org: Organization, kind: str, recipient: str, subject: str, body: str, *,
                employer_id: uuid.UUID | None = None, contractor_id: uuid.UUID | None = None,
                dedupe_key: str | None = None, meta: dict | None = None) -> Notification:
    """Add a QUEUED email to the outbox. Idempotent on ``dedupe_key``: the existing row is returned."""
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"Unknown notification kind {kind!r}.")
    if dedupe_key:
        existing = db.scalar(select(Notification).where(Notification.organization_id == org.id,
                                                        Notification.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
    row = Notification(organization_id=org.id, employer_id=employer_id, contractor_id=contractor_id, kind=kind,
                       channel="email", recipient=recipient.strip(), subject=subject[:300], body=body,
                       status="QUEUED", dedupe_key=dedupe_key, meta=meta)
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # lost a race on the unique key: return the winner
        existing = db.scalar(select(Notification).where(Notification.organization_id == org.id,
                                                        Notification.dedupe_key == dedupe_key))
        if existing is None:
            raise
        return existing
    return row


def deliver(db: Session, notification: Notification, settings: Settings, provider: EmailProvider | None = None
            ) -> Notification:
    """Send a QUEUED (or FAILED, for a retry) notification. Never raises for delivery problems."""
    if notification.status == "SENT":
        return notification
    try:
        provider = provider or provider_for(settings)
        provider.send(recipient=notification.recipient, subject=notification.subject, body=notification.body)
    except ProviderError as exc:
        notification.status, notification.error = "FAILED", str(exc)[:500]
        notification.provider = settings.email_provider
    except Exception as exc:  # noqa: BLE001 - delivery must never crash a request or the sweep
        logger.warning("Email delivery failed (%s).", type(exc).__name__)
        notification.status, notification.error = "FAILED", f"Delivery failed ({type(exc).__name__})."
        notification.provider = getattr(provider, "name", settings.email_provider)
    else:
        notification.status, notification.error = "SENT", None
        notification.provider = provider.name
        notification.sent_at = datetime.now(UTC)
    db.flush()
    return notification
