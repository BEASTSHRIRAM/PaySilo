"""Email one time codes (signup verification, password reset) and the account mails around them.

Security rules, all enforced here:

* The 6 digit code is generated with ``secrets``, emailed as a SENSITIVE notification (never shown by the
  notifications API, audit or logs) and only a keyed HMAC of it is stored. The code itself is never stored.
* A code lives 10 minutes, allows 5 wrong attempts (then it is locked until a new code is sent) and works once.
* Sending is limited: 60 seconds between codes and 5 codes per hour, per email and per client address. The
  limits are applied the same way whether or not the email belongs to an account, so a reset request never
  reveals if an address exists.
* Comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import EmailOtp, Membership, Organization, User
from . import email_templates as tpl
from . import notify

logger = logging.getLogger("paysilo.otp")

CODE_LENGTH = 6
TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_SENDS_PER_HOUR = 5
MAX_GUESSES_PER_HOUR = 25


class OtpError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after


class OtpLimiter:
    """In process sliding windows: sends per email and per client address, plus wrong guesses per email."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._sends: dict[str, deque[float]] = {}
        self._guesses: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _window(self, store: dict, key: str, span: float) -> deque[float]:
        q = store.setdefault(key, deque())
        now = self.clock()
        while q and now - q[0] > span:
            q.popleft()
        return q

    def check_and_record_send(self, email: str, ip: str, purpose: str) -> None:
        with self._lock:
            keys = (f"{purpose}|e|{email}", f"{purpose}|i|{ip}")
            queues = [self._window(self._sends, k, 3600) for k in keys]
            now = self.clock()
            last = max((q[-1] for q in queues[:1] if q), default=None)
            if last is not None and now - last < RESEND_COOLDOWN_SECONDS:
                wait = int(RESEND_COOLDOWN_SECONDS - (now - last)) + 1
                raise OtpError("RESEND_COOLDOWN", f"Please wait {wait} seconds before asking for another code.", 429, wait)
            for q in queues:
                if len(q) >= MAX_SENDS_PER_HOUR:
                    wait = int(3600 - (now - q[0])) + 1
                    raise OtpError("RATE_LIMITED", "Too many codes were requested. Try again later.", 429, wait)
            for q in queues:
                q.append(now)

    def record_guess(self, email: str) -> None:
        with self._lock:
            q = self._window(self._guesses, email, 3600)
            if len(q) >= MAX_GUESSES_PER_HOUR:
                raise OtpError("RATE_LIMITED", "Too many attempts. Try again later.", 429, 3600)
            q.append(self.clock())


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def hash_code(settings: Settings, purpose: str, email: str, code: str) -> str:
    key = ("otp:" + settings.session_secret()).encode()
    return hmac.new(key, f"{purpose}:{email.lower()}:{code}".encode(), hashlib.sha256).hexdigest()


def account_org(db: Session, user: User) -> Organization | None:
    return db.scalar(select(Organization).join(Membership, Membership.organization_id == Organization.id)
                     .where(Membership.user_id == user.id).order_by(Membership.created_at).limit(1))


def queue_account_mail(db: Session, settings: Settings, user: User, template: str, ctx: dict, *, dedupe_key: str,
                       org: Organization | None = None, kind: str | None = None) -> bool:
    """Queue one account mail for ``user`` (their own organisation's outbox). Returns False when it cannot."""
    org = org or account_org(db, user)
    if org is None:
        return False
    spec = tpl.TEMPLATES[template]
    subject, text, html = spec.build({"name": (user.full_name or "").split(" ")[0] or None, **ctx}, org.name)
    notify.queue_email(db, org, kind or spec.kind, user.email, subject, text, body_html=html,
                       dedupe_key=dedupe_key, sensitive=spec.sensitive)
    return True


def send_code(db: Session, settings: Settings, limiter: OtpLimiter, user: User | None, email: str, purpose: str,
              ip: str) -> str | None:
    """Limit, then (when the account exists) create and queue a code. Same outcome for unknown emails."""
    email = email.strip().lower()
    limiter.check_and_record_send(email, ip, purpose)
    if user is None or not user.is_active:
        return None
    now = datetime.now(UTC)
    for old in db.scalars(select(EmailOtp).where(EmailOtp.email == email, EmailOtp.purpose == purpose,
                                                 EmailOtp.consumed_at.is_(None))):
        old.consumed_at = now  # only the newest code works
    code = "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
    row = EmailOtp(user_id=user.id, email=email, purpose=purpose, code_hash=hash_code(settings, purpose, email, code),
                   expires_at=now + TTL, request_ip=ip[:64])
    db.add(row)
    db.flush()
    template = "verify_email_otp" if purpose == "VERIFY_EMAIL" else "password_reset_otp"
    queue_account_mail(db, settings, user, template, {"code": code, "minutes": int(TTL.total_seconds() // 60)},
                       dedupe_key=f"otp:{row.id}")
    db.commit()
    return code  # returned for callers that must not log it; never persisted


def check_code(db: Session, settings: Settings, limiter: OtpLimiter, email: str, purpose: str, code: str) -> EmailOtp:
    """Validate a code. Raises OtpError (OTP_INVALID, OTP_EXPIRED, OTP_LOCKED). Consumes it on success."""
    email = email.strip().lower()
    limiter.record_guess(email)
    row = db.scalar(select(EmailOtp).where(EmailOtp.email == email, EmailOtp.purpose == purpose,
                                           EmailOtp.consumed_at.is_(None)).order_by(EmailOtp.created_at.desc()).limit(1))
    invalid = OtpError("OTP_INVALID", "That code is not right. Check it and try again.")
    if row is None:
        raise invalid
    if _utc(row.expires_at) <= datetime.now(UTC):
        raise OtpError("OTP_EXPIRED", "That code has expired. Request a new one.")
    if row.attempts >= MAX_ATTEMPTS:
        raise OtpError("OTP_LOCKED", "Too many wrong attempts. Request a new code.")
    expected = hash_code(settings, purpose, email, "".join(ch for ch in code if ch.isdigit()))
    if not hmac.compare_digest(expected, row.code_hash):
        row.attempts += 1
        db.commit()
        if row.attempts >= MAX_ATTEMPTS:
            raise OtpError("OTP_LOCKED", "Too many wrong attempts. Request a new code.")
        raise invalid
    row.consumed_at = datetime.now(UTC)
    db.flush()
    return row
