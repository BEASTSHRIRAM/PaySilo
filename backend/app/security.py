"""Passwords, session tokens and login throttling.

- Passwords: bcrypt (cost 12). bcrypt only reads 72 bytes, so longer
  passwords are rejected at validation time instead of silently truncated.
- Sessions: short HS256 JWTs carrying user id + organization id, delivered
  as an HttpOnly, SameSite=Lax cookie (``Authorization: Bearer`` is also
  accepted for API clients such as the AgentCore runtime).
- Login throttling: per (email, client IP), in-process sliding window.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

SESSION_COOKIE = "paysilo_session"
JWT_ALGORITHM = "HS256"
BCRYPT_MAX_BYTES = 72
_DUMMY_HASH = bcrypt.hashpw(b"paysilo-timing-equaliser", bcrypt.gensalt(rounds=12)).decode()


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError("Password is too long (max 72 bytes).")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    raw = password.encode("utf-8")
    if password_hash is None or len(raw) > BCRYPT_MAX_BYTES:
        bcrypt.checkpw(b"x", _DUMMY_HASH.encode())  # keep timing similar for unknown users
        return False
    return bcrypt.checkpw(raw, password_hash.encode("ascii"))


@dataclass(frozen=True)
class SessionClaims:
    user_id: uuid.UUID
    organization_id: uuid.UUID


def create_session_token(claims: SessionClaims, secret: str, ttl_minutes: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(claims.user_id),
        "org": str(claims.organization_id),
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "iss": "paysilo",
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_session_token(token: str, secret: str) -> SessionClaims | None:
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], issuer="paysilo",
                             options={"require": ["exp", "sub", "org"]})
        return SessionClaims(user_id=uuid.UUID(payload["sub"]), organization_id=uuid.UUID(payload["org"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


class LoginThrottle:
    """Allow ``limit`` failed attempts per key within ``window`` seconds."""

    def __init__(self, limit: int = 8, window: float = 300.0):
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until the key may try again; 0 when allowed."""
        with self._lock:
            now = time.monotonic()
            q = self._prune(key, now)
            if len(q) < self.limit:
                return 0
            return max(1, int(self.window - (now - q[0])))

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
