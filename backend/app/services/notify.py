"""Notifications outbox: queue a message, deliver it later from a background sweep.

Request handlers only QUEUE rows (cheap, idempotent on ``dedupe_key``); delivery happens in
:func:`deliver_pending` on a background thread/loop, so a slow mail server never blocks a request.

Mailers are pluggable:

* :class:`SmtpMailer` - real delivery over SMTP with STARTTLS (Brevo: smtp-relay.brevo.com:587), stdlib only.
* :class:`OutboxOnlyMailer` - used when no mail credentials are configured: messages stay QUEUED and
  nothing leaves the machine. The UI says so honestly.

A failed send records the last error (never credentials) and is retried with backoff up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import NOTIFICATION_KINDS, Notification, Organization

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = (1, 5, 15, 60)  # wait before attempt 2, 3, 4, 5


class MailerError(Exception):
    """A mailer could not deliver. The message must already be safe to store and show."""


class Mailer(Protocol):
    name: str
    configured: bool

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None: ...


class OutboxOnlyMailer:
    """No credentials: messages are only queued."""

    name = "outbox"
    configured = False

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        raise MailerError("Email delivery is not configured.")


class SmtpMailer:
    name = "smtp"
    configured = True

    def __init__(self, host: str, port: int, user: str, key: str, from_addr: str, from_name: str = "PaySilo",
                 smtp_factory=None):
        self.host, self.port, self.user, self._key = host, port, user, key
        self.from_addr, self.from_name = from_addr, from_name
        self._factory = smtp_factory or smtplib.SMTP

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def send(self, *, to: str, subject: str, text: str, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = formataddr((self.from_name, self.from_addr)), to, subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            with self._factory(self.host, self.port, timeout=20) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(self.user, self._key)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise MailerError("The mail server rejected the SMTP login (check the Brevo SMTP user and key).") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailerError("The mail server refused the recipient address.") from None
        except smtplib.SMTPSenderRefused:
            raise MailerError("The mail server refused the sender (EMAIL_FROM must be a verified Brevo sender).") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise MailerError(self._scrub(
                f"The mail server could not be reached or refused the message ({type(exc).__name__}).")) from None


def mailer_for(settings: Settings) -> Mailer:
    if settings.email_configured:
        return SmtpMailer(settings.brevo_smtp_host, settings.brevo_smtp_port, settings.brevo_smtp_user,
                          settings.brevo_smtp_key, settings.email_from, settings.email_from_name)
    return OutboxOnlyMailer()


def email_status(settings: Settings) -> dict:
    return {"configured": settings.email_configured, "provider": "brevo-smtp" if settings.email_configured else None,
            "message": None if settings.email_configured else
            "Email delivery: not configured. Messages are queued here and not sent."}


def queue_email(db: Session, org: Organization, kind: str, recipient: str, subject: str, body: str, *,
                body_html: str | None = None, employer_id: uuid.UUID | None = None,
                contractor_id: uuid.UUID | None = None, dedupe_key: str | None = None,
                meta: dict | None = None) -> Notification:
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
                       body_html=body_html, status="QUEUED", attempts=0, dedupe_key=dedupe_key, meta=meta)
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


def deliver(db: Session, notification: Notification, mailer: Mailer) -> Notification:
    """One delivery attempt. A no-op for an unconfigured mailer; never raises for delivery problems."""
    if notification.status == "SENT" or not mailer.configured:
        return notification
    now = datetime.now(UTC)
    notification.attempts = (notification.attempts or 0) + 1
    notification.last_attempt_at = now
    notification.provider = mailer.name
    try:
        if not notification.recipient:
            raise MailerError("No recipient email address.")
        mailer.send(to=notification.recipient, subject=notification.subject, text=notification.body,
                    html=notification.body_html)
    except MailerError as exc:
        notification.status, notification.error = "FAILED", str(exc)[:500]
    except Exception as exc:  # noqa: BLE001 - delivery must never crash the sweep
        logger.warning("Email delivery failed (%s).", type(exc).__name__)
        notification.status, notification.error = "FAILED", f"Delivery failed ({type(exc).__name__})."
    else:
        notification.status, notification.error, notification.sent_at = "SENT", None, now
    db.flush()
    return notification


def _due(n: Notification, now: datetime) -> bool:
    if n.status == "QUEUED":
        return True
    if n.status != "FAILED" or (n.attempts or 0) >= MAX_ATTEMPTS or n.last_attempt_at is None:
        return False
    last = n.last_attempt_at if n.last_attempt_at.tzinfo else n.last_attempt_at.replace(tzinfo=UTC)
    wait = RETRY_BACKOFF_MINUTES[min(max(n.attempts - 1, 0), len(RETRY_BACKOFF_MINUTES) - 1)]
    return now - last >= timedelta(minutes=wait)


_deliver_lock = threading.Lock()


def deliver_pending(db_factory, mailer: Mailer, *, limit: int = 50, now: datetime | None = None) -> int:
    """Send queued rows and retry failed ones (with backoff). Returns the number sent. Error-safe."""
    if not mailer.configured or not _deliver_lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        now = now or datetime.now(UTC)
        with db_factory() as s:
            rows = s.scalars(select(Notification).where(
                or_(Notification.status == "QUEUED",
                    (Notification.status == "FAILED") & (Notification.attempts < MAX_ATTEMPTS)))
                .order_by(Notification.created_at).limit(limit * 4)).all()
            for n in [r for r in rows if _due(r, now)][:limit]:
                deliver(s, n, mailer)
                sent += n.status == "SENT"
                s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Delivering queued email failed.")
    finally:
        _deliver_lock.release()
    return sent


def dispatch_async(db_factory, settings: Settings) -> None:
    """Kick delivery off on a daemon thread so the request that queued the mail returns immediately."""
    mailer = mailer_for(settings)
    if mailer.configured:
        threading.Thread(target=deliver_pending, args=(db_factory, mailer), daemon=True,
                         name="paysilo-mail").start()
