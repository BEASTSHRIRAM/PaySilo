"""Organization sign-up, login, logout and the current session."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import Membership, Organization, User
from ...security import (
    SESSION_COOKIE,
    LoginThrottle,
    SessionClaims,
    create_session_token,
    hash_password,
    verify_password,
)
from ...services.seed import DEMO_EMAIL, DEMO_ORG_NAME, DEMO_PASSWORD
from ..deps import Tenant, current_tenant, get_db, get_settings
from ..schemas import DemoCredentialsOut, LoginIn, OrganizationOut, PasswordIn, ProfileIn, SessionOut, SignupIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:48] or "org"


def _unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while db.scalar(select(Organization.id).where(Organization.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def _set_session(response: Response, settings: Settings, user: User, org: Organization) -> None:
    token = create_session_token(SessionClaims(user.id, org.id), settings.session_secret(),
                                 settings.session_ttl_minutes)
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.session_ttl_minutes * 60, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")


def _session_out(user: User, org: Organization, role: str) -> SessionOut:
    return SessionOut(user=UserOut.model_validate(user), organization=OrganizationOut.model_validate(org), role=role)


@router.post("/signup", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, response: Response, db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)) -> SessionOut:
    email = body.admin.email.lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Sign in instead.")
    org = Organization(name=body.organization.name, slug=_unique_slug(db, body.organization.name),
                       legal_name=body.organization.legal_name, country=body.organization.country,
                       base_currency=body.organization.base_currency, industry=body.organization.industry,
                       size_band=body.organization.size_band, website=body.organization.website)
    user = User(email=email, full_name=body.admin.full_name.strip(),
                password_hash=hash_password(body.admin.password), last_login_at=datetime.now(UTC))
    db.add_all([org, user])
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="owner"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That organization or email is already registered.") from None
    _set_session(response, settings, user, org)
    return _session_out(user, org, "owner")


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)) -> SessionOut:
    email = body.email.strip().lower()
    key = f"{email}|{request.client.host if request.client else '-'}"
    throttle: LoginThrottle = request.app.state.login_throttle
    wait = throttle.retry_after(key)
    if wait:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not verify_password(body.password, user.password_hash if user else None) or not user or not user.is_active:
        throttle.record_failure(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect.")
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id)
                           .order_by(Membership.created_at).limit(1))
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This user does not belong to an organization yet.")
    throttle.reset(key)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    org = db.get(Organization, membership.organization_id)
    _set_session(response, settings, user, org)
    return _session_out(user, org, membership.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, settings: Settings = Depends(get_settings)) -> Response:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax", httponly=True)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=SessionOut)
def me(tenant: Tenant = Depends(current_tenant)) -> SessionOut:
    return _session_out(tenant.user, tenant.organization, tenant.role)


@router.patch("/me", response_model=SessionOut)
def update_me(body: ProfileIn, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> SessionOut:
    tenant.user.full_name = body.full_name.strip()
    db.commit()
    db.refresh(tenant.user)
    return _session_out(tenant.user, tenant.organization, tenant.role)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordIn, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> Response:
    if not verify_password(body.current_password, tenant.user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    tenant.user.password_hash = hash_password(body.new_password)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/demo-credentials", response_model=DemoCredentialsOut)
def demo_credentials(settings: Settings = Depends(get_settings)) -> DemoCredentialsOut:
    """Only exposed while the demo organization is seeded (never in live mode)."""
    if not settings.seed_demo or settings.app_env == "live":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demo credentials are not available.")
    return DemoCredentialsOut(email=DEMO_EMAIL, password=DEMO_PASSWORD, organization=DEMO_ORG_NAME)

