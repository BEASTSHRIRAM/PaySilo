"""Organization sign-up, login, logout and the current session."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
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
from ...services import notify, otp
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


class EmailIn(BaseModel):
    email: EmailStr


class VerifyEmailIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=12)


class ResetPasswordIn(VerifyEmailIn):
    new_password: str = Field(min_length=8, max_length=72)


def _limiter(request: Request) -> otp.OtpLimiter:
    if not hasattr(request.app.state, "otp_limiter"):
        request.app.state.otp_limiter = otp.OtpLimiter()
    return request.app.state.otp_limiter


def _ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def _otp_http(exc: otp.OtpError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message}, headers=headers)


def _app(settings: Settings, path: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}{path}"


def _send_verification(request: Request, db: Session, settings: Settings, user: User) -> None:
    otp.send_code(db, settings, _limiter(request), user, user.email, "VERIFY_EMAIL", _ip(request))
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, request: Request, response: Response, db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)) -> SessionOut | dict:
    email = body.admin.email.lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Sign in instead.")
    org = Organization(name=body.organization.name, slug=_unique_slug(db, body.organization.name),
                       legal_name=body.organization.legal_name, country=body.organization.country,
                       base_currency=body.organization.base_currency, industry=body.organization.industry,
                       size_band=body.organization.size_band, website=body.organization.website)
    needs_code = settings.email_verification_required
    user = User(email=email, full_name=body.admin.full_name.strip(),
                password_hash=hash_password(body.admin.password), last_login_at=datetime.now(UTC),
                email_verified_at=None if needs_code else datetime.now(UTC))
    db.add_all([org, user])
    db.flush()
    db.add(Membership(organization_id=org.id, user_id=user.id, role="owner"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That organization or email is already registered.") from None
    if needs_code:  # no session until the address is proven
        try:
            _send_verification(request, db, settings, user)
        except otp.OtpError as exc:
            raise _otp_http(exc) from None
        return {"verification_required": True, "email": email, "resend_in": otp.RESEND_COOLDOWN_SECONDS}
    if settings.email_configured:
        otp.queue_account_mail(db, settings, user, "account_welcome",
                               {"org_name": org.name, "url": _app(settings, "/dashboard")},
                               dedupe_key=f"welcome:{user.id}", org=org)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    _set_session(response, settings, user, org)
    return _session_out(user, org, "owner")


@router.post("/verify-email", response_model=SessionOut)
def verify_email(body: VerifyEmailIn, request: Request, response: Response, db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)) -> SessionOut:
    email = str(body.email).lower()
    try:
        row = otp.check_code(db, settings, _limiter(request), email, "VERIFY_EMAIL", body.code)
    except otp.OtpError as exc:
        raise _otp_http(exc) from None
    user = db.get(User, row.user_id) if row.user_id else None
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)
                           .limit(1)) if user else None
    if user is None or membership is None or not user.is_active:
        raise _otp_http(otp.OtpError("OTP_INVALID", "That code is not right. Check it and try again."))
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)
    user.last_login_at = datetime.now(UTC)
    org = db.get(Organization, membership.organization_id)
    otp.queue_account_mail(db, settings, user, "account_welcome",
                           {"org_name": org.name, "url": _app(settings, "/dashboard")},
                           dedupe_key=f"welcome:{user.id}", org=org)
    db.commit()
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    _set_session(response, settings, user, org)
    return _session_out(user, org, membership.role)


@router.post("/resend-otp")
def resend_otp(body: EmailIn, request: Request, db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> dict:
    """Always the same answer, so it never reveals whether an account exists or is already verified."""
    email = str(body.email).lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is not None and user.email_verified_at is not None:
        user = None
    try:
        otp.send_code(db, settings, _limiter(request), user, email, "VERIFY_EMAIL", _ip(request))
    except otp.OtpError as exc:
        raise _otp_http(exc) from None
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return {"sent": True, "resend_in": otp.RESEND_COOLDOWN_SECONDS}


@router.post("/forgot-password")
def forgot_password(body: EmailIn, request: Request, db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> dict:
    email = str(body.email).lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    try:
        otp.send_code(db, settings, _limiter(request), user, email, "RESET_PASSWORD", _ip(request))
    except otp.OtpError as exc:
        raise _otp_http(exc) from None
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return {"sent": True, "resend_in": otp.RESEND_COOLDOWN_SECONDS}


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, request: Request, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> dict:
    pw = body.new_password
    if len(pw.encode()) > 72 or not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)):
        raise HTTPException(422, "Password must be 8+ characters with a letter and a number (max 72 bytes).")
    email = str(body.email).lower()
    try:
        row = otp.check_code(db, settings, _limiter(request), email, "RESET_PASSWORD", body.code)
    except otp.OtpError as exc:
        raise _otp_http(exc) from None
    user = db.get(User, row.user_id) if row.user_id else None
    if user is None or not user.is_active:
        raise _otp_http(otp.OtpError("OTP_INVALID", "That code is not right. Check it and try again."))
    user.password_hash = hash_password(pw)
    user.email_verified_at = user.email_verified_at or datetime.now(UTC)  # the code proved the address
    otp.queue_account_mail(db, settings, user, "password_changed",
                           {"when": datetime.now(UTC).date().isoformat(), "url": _app(settings, "/forgot-password")},
                           dedupe_key=f"pwchanged:{row.id}")
    db.commit()
    request.app.state.login_throttle.reset(f"{email}|{_ip(request)}")
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return {"ok": True}


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
    if user.email_verified_at is None and settings.email_verification_required:
        try:  # a fresh code goes out (subject to the resend limits) so the UI can show the code screen
            _send_verification(request, db, settings, user)
        except otp.OtpError:
            pass
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "EMAIL_NOT_VERIFIED",
                                                        "message": "Verify your email to sign in. We sent you a code."})
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
def change_password(body: PasswordIn, request: Request, tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> Response:
    if not verify_password(body.current_password, tenant.user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    tenant.user.password_hash = hash_password(body.new_password)
    stamp = datetime.now(UTC)
    settings = request.app.state.settings
    otp.queue_account_mail(db, settings, tenant.user, "password_changed",
                           {"when": stamp.date().isoformat(), "url": _app(settings, "/forgot-password")},
                           dedupe_key=f"pwchanged:{tenant.user.id}:{stamp.isoformat()}", org=tenant.organization)
    db.commit()
    notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/demo-credentials", response_model=DemoCredentialsOut)
def demo_credentials(settings: Settings = Depends(get_settings)) -> DemoCredentialsOut:
    """Only exposed while the demo organization is seeded (never in live mode)."""
    if not settings.seed_demo or settings.app_env == "live":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Demo credentials are not available.")
    return DemoCredentialsOut(email=DEMO_EMAIL, password=DEMO_PASSWORD, organization=DEMO_ORG_NAME)

