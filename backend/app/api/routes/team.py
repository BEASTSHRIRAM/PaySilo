from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...db.models import Invitation, Membership, Organization, User
from ...security import hash_password, verify_password
from ...services import email_templates, notify
from ..deps import Tenant, current_tenant, get_db, get_settings, require_roles
from ..schemas import SessionOut
from .auth import _set_session, _session_out

router = APIRouter(tags=["organization team"])

class RoleIn(BaseModel):
    role: Literal["owner", "admin", "approver", "viewer"]
class InviteIn(BaseModel):
    email: EmailStr
    role: Literal["admin", "approver", "viewer"]
class JoinNewIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("full_name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("must contain at least 2 characters")
        return value
class JoinExistingIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def strong(cls, v: str) -> str:
        if len(v.encode()) > 72 or not any(c.isalpha() and c.isascii() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a letter and number and be at most 72 bytes.")
        return v

class PublicInviteOut(BaseModel):
    organization_name: str
    email: str
    role: str
    invited_by: str | None
    existing_user: bool
    expires_at: datetime

def _invite_or_404(token: str, db: Session) -> Invitation:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invite = db.scalar(select(Invitation).where(Invitation.token_hash == token_hash))
    if not invite or not hmac.compare_digest(invite.token_hash, token_hash) or invite.status != "PENDING":
        raise HTTPException(404, "Invitation not found.")
    expiry = invite.expires_at.replace(tzinfo=UTC) if invite.expires_at.tzinfo is None else invite.expires_at
    if expiry <= datetime.now(UTC):
        raise HTTPException(404, "Invitation not found.")
    return invite

def _invite_data(inv: Invitation, raw: str | None = None):
    data = {"id": inv.id, "email": inv.email, "role": inv.role, "status": inv.status,
            "expires_at": inv.expires_at, "created_at": inv.created_at}
    if raw is not None:
        data["invite_path"] = f"/invite/{raw}"
    return data

@router.get("/org/members")
def members(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)):
    rows = db.execute(select(User, Membership).join(Membership, Membership.user_id == User.id)
                      .where(Membership.organization_id == tenant.organization.id).order_by(Membership.created_at)).all()
    return [{"id": u.id, "name": u.full_name, "email": u.email, "role": m.role, "joined_at": m.created_at} for u, m in rows]

@router.patch("/org/members/{user_id}")
def update_member(user_id: uuid.UUID, body: RoleIn, tenant: Tenant = Depends(require_roles({"owner"})), db: Session = Depends(get_db)):
    if user_id == tenant.user.id:
        raise HTTPException(400, "You cannot change your own role.")
    m = db.scalar(select(Membership).where(Membership.organization_id == tenant.organization.id, Membership.user_id == user_id))
    if not m: raise HTTPException(404, "Member not found.")
    if m.role == "owner" or body.role == "owner":
        owners = db.scalar(select(func.count()).select_from(Membership).where(Membership.organization_id == tenant.organization.id, Membership.role == "owner")) or 0
        if m.role == "owner" and body.role != "owner" and owners <= 1: raise HTTPException(400, "The last owner cannot be demoted.")
    m.role = body.role
    db.commit()
    return {"id": m.user_id, "role": m.role}

@router.delete("/org/members/{user_id}", status_code=204)
def remove_member(user_id: uuid.UUID, tenant: Tenant = Depends(require_roles({"owner", "admin"})), db: Session = Depends(get_db)):
    if user_id == tenant.user.id: raise HTTPException(400, "You cannot remove yourself.")
    m = db.scalar(select(Membership).where(Membership.organization_id == tenant.organization.id, Membership.user_id == user_id))
    if not m: raise HTTPException(404, "Member not found.")
    if m.role == "owner":
        if tenant.role != "owner": raise HTTPException(403, "Admins cannot remove owners.")
        owners = db.scalar(select(func.count()).select_from(Membership).where(Membership.organization_id == tenant.organization.id, Membership.role == "owner")) or 0
        if owners <= 1: raise HTTPException(400, "The last owner cannot be removed.")
    db.delete(m); db.commit(); return Response(status_code=204)

@router.post("/org/invitations", status_code=201)
def create_invitation(body: InviteIn, request: Request, tenant: Tenant = Depends(require_roles({"owner", "admin"})),
                      db: Session = Depends(get_db), settings=Depends(get_settings)):
    if tenant.role == "admin" and body.role == "admin": raise HTTPException(403, "Admins cannot invite admins.")
    email = str(body.email).lower()
    if db.scalar(select(Membership.id).join(User, User.id == Membership.user_id).where(Membership.organization_id == tenant.organization.id, func.lower(User.email) == email)):
        raise HTTPException(409, "This email already belongs to a member.")
    if db.scalar(select(Invitation.id).where(Invitation.organization_id == tenant.organization.id, Invitation.email == email,
                             Invitation.status == "PENDING", Invitation.expires_at > datetime.now(UTC))):
        raise HTTPException(409, "A pending invitation already exists for this email.")
    token = secrets.token_urlsafe(32)
    inv = Invitation(organization_id=tenant.organization.id, email=email, role=body.role,
                     token_hash=hashlib.sha256(token.encode()).hexdigest(), invited_by_id=tenant.user.id,
                     status="PENDING", expires_at=datetime.now(UTC) + timedelta(days=7))
    db.add(inv); db.commit(); db.refresh(inv)
    # Email the link through the outbox as a SENSITIVE message: the token never appears in the notifications list.
    try:
        url = f"{settings.app_base_url.rstrip('/')}/invite/{token}"
        subject, text, html = email_templates.invite(tenant.organization.name, tenant.user.full_name, inv.role, url)
        notify.queue_email(db, tenant.organization, "INVITE", email, subject, text, body_html=html,
                           dedupe_key=f"invite:{inv.id}", meta={"invitation_id": str(inv.id)}, sensitive=True)
        db.commit()
        notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    except Exception:  # noqa: BLE001 - a mail problem must not undo the invitation; the link is still returned
        db.rollback()
    return _invite_data(inv, token)

@router.get("/org/invitations")
def list_invitations(tenant: Tenant = Depends(require_roles({"owner", "admin"})), db: Session = Depends(get_db)):
    rows = db.scalars(select(Invitation).where(Invitation.organization_id == tenant.organization.id,
                         Invitation.status == "PENDING", Invitation.expires_at > datetime.now(UTC))
                      .order_by(Invitation.created_at.desc())).all()
    return [_invite_data(i) for i in rows]

@router.post("/org/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: uuid.UUID, tenant: Tenant = Depends(require_roles({"owner", "admin"})), db: Session = Depends(get_db)):
    inv = db.scalar(select(Invitation).where(Invitation.id == invitation_id, Invitation.organization_id == tenant.organization.id, Invitation.status == "PENDING"))
    if not inv: raise HTTPException(404, "Invitation not found.")
    inv.status = "REVOKED"; db.commit(); return {"id": inv.id, "status": inv.status}

@router.get("/invitations/{token}", response_model=PublicInviteOut)
def public_invitation(token: str, db: Session = Depends(get_db)):
    inv = _invite_or_404(token, db); org = db.get(Organization, inv.organization_id)
    inviter = db.get(User, inv.invited_by_id) if inv.invited_by_id else None
    existing = db.scalar(select(User.id).where(func.lower(User.email) == inv.email)) is not None
    return PublicInviteOut(organization_name=org.name, email=inv.email, role=inv.role,
                           invited_by=inviter.full_name if inviter else None, existing_user=existing, expires_at=inv.expires_at)

@router.post("/invitations/{token}/accept", response_model=SessionOut)
def accept_invitation(token: str, response: Response, body: dict, db: Session = Depends(get_db), settings=Depends(get_settings)):
    inv = _invite_or_404(token, db)
    email = inv.email
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user:
        try: payload = JoinExistingIn.model_validate(body)
        except Exception as e: raise HTTPException(422, str(e)) from None
        if not verify_password(payload.password, user.password_hash): raise HTTPException(400, "Password is incorrect.")
    else:
        try: payload = JoinNewIn.model_validate(body)
        except Exception as e: raise HTTPException(422, str(e)) from None
        pwd = payload.password
        if len(pwd.encode()) > 72 or not any(c.isascii() and c.isalpha() for c in pwd) or not any(c.isdigit() for c in pwd):
            raise HTTPException(422, "Password must be 8+ characters and contain a letter and a number (max 72 bytes).")
        user = User(email=email, full_name=payload.full_name.strip(), password_hash=hash_password(pwd))
        db.add(user); db.flush()
    org = db.get(Organization, inv.organization_id)
    try:
        db.add(Membership(organization_id=inv.organization_id, user_id=user.id, role=inv.role))
        inv.status = "ACCEPTED"; inv.accepted_at = datetime.now(UTC)
        db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(404, "Invitation not found.") from None
    _set_session(response, settings, user, org)
    return _session_out(user, org, inv.role)
