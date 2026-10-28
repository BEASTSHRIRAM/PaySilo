"""FastAPI dependencies: DB session, runtime, and the authenticated tenant."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Membership, Organization, User
from ..security import SESSION_COOKIE, decode_session_token
from ..services.runtime import PlatformRuntime

APPROVER_ROLES = {"owner", "admin", "approver"}
EDITOR_ROLES = {"owner", "admin"}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_runtime(request: Request) -> PlatformRuntime:
    return request.app.state.runtime


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


@dataclass
class Tenant:
    user: User
    organization: Organization
    role: str


def _token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


def current_tenant(request: Request, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> Tenant:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.",
                                 headers={"WWW-Authenticate": "Bearer"})
    token = _token(request)
    claims = decode_session_token(token, settings.session_secret()) if token else None
    if claims is None:
        raise unauthorized
    row = db.execute(
        select(User, Organization, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(User.id == claims.user_id, Organization.id == claims.organization_id)
    ).first()
    if row is None or not row[0].is_active:
        raise unauthorized
    return Tenant(user=row[0], organization=row[1], role=row[2])


def require_roles(roles: set[str]):
    def checker(tenant: Tenant = Depends(current_tenant)) -> Tenant:
        if tenant.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Your role ({tenant.role}) cannot do this.")
        return tenant
    return checker
