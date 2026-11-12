"""Contractor trust: change control for bank details, self-serve onboarding, tax/compliance tracking.

Bank-detail changes are the classic payment-fraud vector, so any change makes the contractor
PENDING_VERIFICATION until a DIFFERENT person confirms it (after an independent callback).
The public onboarding portal can only *submit* details - it never makes them payable.
Only statuses and dates are stored about tax: never tax ids or documents.
See ``backend/docs/contractor-trust.md``.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import Settings
from ...db.models import (
    Contractor,
    ContractorChange,
    ContractorOnboardingInvite,
    Employer,
    Organization,
    PayLine,
    PayrollRun,
    User,
)
from ...money import money_str
from ...security import LoginThrottle
from ...services.contractor_trust import (
    BANK_FIELDS,
    PAYOUT_OPTIONS,
    TAX_EXPIRY_WARNING_DAYS,
    aware,
    bank_change_summary,
    bank_snapshot,
    effective_tax_form_status,
    mask,
    over_1099_threshold,
    today_utc,
    validate_bank,
    ytd_paid,
)
from ...services.runtime import PlatformRuntime
from ...services import notify, trust_notify
from ..deps import APPROVER_ROLES, EDITOR_ROLES, Tenant, current_tenant, get_db, get_runtime, get_settings, require_roles
from ..schemas import ContractorIn, ContractorOut
from ..serializers import contractor_out

router = APIRouter(tags=["contractor trust"])

INVITE_TTL_DAYS = 14
NOT_TAX_ADVICE = ("PaySilo tracks form status and dates only, never tax ids or documents. "
                  "This is not tax advice - confirm requirements with your tax advisor.")


# ------------------------------------------------------------------ shared helpers
def _contractor_or_404(db: Session, tenant: Tenant, employer_id: uuid.UUID, contractor_id: uuid.UUID) -> tuple[Employer, Contractor]:
    row = db.execute(
        select(Employer, Contractor).join(Contractor, Contractor.employer_id == Employer.id)
        .where(Employer.id == employer_id, Employer.organization_id == tenant.organization.id,
               Contractor.id == contractor_id, Contractor.organization_id == tenant.organization.id)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contractor not found.")
    return row[0], row[1]


def _log(db: Session, c: Contractor, group: str, summary: str, user_id: uuid.UUID | None) -> None:
    db.add(ContractorChange(organization_id=c.organization_id, contractor_id=c.id, change_group=group,
                            summary=summary, changed_by_id=user_id))


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = str(err["msg"]).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ PATCH contractor
TaxStatus = Literal["US_PERSON", "NON_US", "UNKNOWN"]
TaxFormType = Literal["W9", "W8BEN", "W8BENE", "NONE"]
TaxFormStatus = Literal["MISSING", "RECEIVED", "EXPIRED"]


class ContractorPatchIn(BaseModel):
    """Every field optional; only the fields sent are changed. Empty bank fields clear them."""

    model_config = ConfigDict(extra="forbid")
    full_name: str | None = None
    email: str | None = None
    country: str | None = None
    currency: str | None = None
    payout_method: Literal["SWIFT", "LOCAL"] | None = None
    iban: str | None = None
    swift_code: str | None = None
    account_number: str | None = None
    routing_code: str | None = None
    default_amount: str | None = None
    tax_status: TaxStatus | None = None
    tax_form_type: TaxFormType | None = None
    tax_form_status: TaxFormStatus | None = None
    tax_form_received_at: date | None = None
    tax_form_expires_at: date | None = None


class BankValidationOut(BaseModel):
    valid: bool
    checked: bool = True  # False when the provider could not be reached
    errors: list[str] = []


class ContractorPatchOut(BaseModel):
    contractor: ContractorOut
    bank_change_pending: bool
    bank_validation: BankValidationOut | None = None


_PROFILE_KEYS = ("full_name", "email", "country", "default_amount")
_TAX_KEYS = ("tax_status", "tax_form_type", "tax_form_status", "tax_form_received_at", "tax_form_expires_at")


@router.patch("/employers/{employer_id}/contractors/{contractor_id}", response_model=ContractorPatchOut)
def patch_contractor(employer_id: uuid.UUID, contractor_id: uuid.UUID, body: ContractorPatchIn, request: Request,
                     tenant: Tenant = Depends(require_roles(EDITOR_ROLES)), db: Session = Depends(get_db),
                     runtime: PlatformRuntime = Depends(get_runtime),
                     settings: Settings = Depends(get_settings)) -> ContractorPatchOut:
    emp, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    sent = {k: getattr(body, k) for k in body.model_fields_set}

    # Profile + bank fields: validate the MERGED record with the same rules as "add contractor".
    merged = {"full_name": c.full_name, "email": c.email, "country": c.country, "currency": c.currency,
              "payout_method": c.payout_method, "iban": c.iban, "swift_code": c.swift_code,
              "account_number": c.account_number, "routing_code": c.routing_code,
              "default_amount": str(c.default_amount) if c.default_amount is not None else None}
    for key in (*_PROFILE_KEYS, "currency", "payout_method", "iban", "swift_code", "account_number", "routing_code"):
        if key in sent:
            value = sent[key]
            merged[key] = None if isinstance(value, str) and value.strip() == "" else value
    try:
        valid = ContractorIn(**merged)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _validation_message(exc)) from None

    old_bank = bank_snapshot(c)
    notes: list[str] = []
    for key in ("full_name", "country"):
        new = getattr(valid, key)
        if new != getattr(c, key):
            notes.append(f"{'Name' if key == 'full_name' else 'Country'} {getattr(c, key)} → {new}")
            setattr(c, key, new)
    if (valid.email or None) != c.email:
        notes.append("Email updated")
        c.email = valid.email
    new_amount = Decimal(valid.default_amount) if valid.default_amount else None
    if new_amount != c.default_amount:
        notes.append(f"Default amount {money_str(c.default_amount) if c.default_amount else 'none'} → "
                     f"{money_str(new_amount) if new_amount else 'none'}")
        c.default_amount = new_amount
    for key in BANK_FIELDS:
        setattr(c, key, getattr(valid, key))
    new_bank = bank_snapshot(c)
    bank_changed = old_bank != new_bank
    if notes:
        _log(db, c, "profile", "; ".join(notes), tenant.user.id)

    # Tax fields (staff-attested). Status/dates only.
    tax_notes: list[str] = []
    for key in _TAX_KEYS:
        if key in sent and sent[key] != getattr(c, key):
            tax_notes.append(f"{key.replace('_', ' ').capitalize()} {getattr(c, key) or 'none'} → {sent[key] or 'none'}")
            setattr(c, key, sent[key])
    if c.tax_form_status == "RECEIVED":
        if c.tax_form_type == "NONE":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose which tax form was received (W-9, W-8BEN or W-8BEN-E).")
        if c.tax_form_received_at is None:
            c.tax_form_received_at = today_utc()
    if c.tax_form_expires_at and c.tax_form_received_at and c.tax_form_expires_at < c.tax_form_received_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The form expiry date is before the date it was received.")
    if tax_notes:
        _log(db, c, "tax", "; ".join(tax_notes), tenant.user.id)

    validation: BankValidationOut | None = None
    if bank_changed:
        now = datetime.now(UTC)
        c.bank_verification_status = "PENDING_VERIFICATION"
        c.bank_details_changed_at = now
        c.bank_change_requested_by_id = tenant.user.id
        c.bank_verified_by_id = None
        c.bank_verified_at = None
        _log(db, c, "bank", bank_change_summary(old_bank, new_bank), tenant.user.id)
        validation = BankValidationOut(**validate_bank(runtime, emp, c))
    db.commit()
    if bank_changed:  # approvers get a "confirm with the contractor by callback" request
        if trust_notify.bank_change_callback_request(db, settings, tenant.organization, emp, c):
            notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return ContractorPatchOut(contractor=contractor_out(c), bank_change_pending=c.bank_verification_status == "PENDING_VERIFICATION",
                              bank_validation=validation)


# ------------------------------------------------------------------ four-eyes verification
class VerifyBankIn(BaseModel):
    callback_confirmed: bool

    @field_validator("callback_confirmed")
    @classmethod
    def _must_confirm(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("Confirm that you verified this change with the contractor through a known, independent channel.")
        return v


@router.post("/employers/{employer_id}/contractors/{contractor_id}/verify-bank", response_model=ContractorOut)
def verify_bank(employer_id: uuid.UUID, contractor_id: uuid.UUID, body: VerifyBankIn,
                tenant: Tenant = Depends(require_roles(APPROVER_ROLES)), db: Session = Depends(get_db),
                runtime: PlatformRuntime = Depends(get_runtime)) -> ContractorOut:
    emp, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    if c.bank_verification_status != "PENDING_VERIFICATION":
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no bank change waiting for verification.")
    if c.bank_change_requested_by_id is not None and c.bank_change_requested_by_id == tenant.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "You made this bank change, so a second person must verify it. Ask a colleague "
                            "(owner, admin or approver) to confirm it with the contractor.")
    check = validate_bank(runtime, emp, c)
    if not check["checked"]:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The bank details could not be checked right now. Try again shortly.")
    if not check["valid"]:
        fields = ", ".join(check["errors"]) or "bank details"
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"These bank details don't pass validation (fields: {fields}). Correct them first.")
    c.bank_verification_status = "VERIFIED"
    c.bank_verified_by_id = tenant.user.id
    c.bank_verified_at = datetime.now(UTC)
    _log(db, c, "bank", f"Bank details verified (account {mask(c.iban or c.account_number)}) after callback confirmation",
         tenant.user.id)
    db.commit()
    return contractor_out(c)


@router.get("/employers/{employer_id}/contractors/{contractor_id}/bank-check", response_model=BankValidationOut)
def bank_check(employer_id: uuid.UUID, contractor_id: uuid.UUID, tenant: Tenant = Depends(require_roles(APPROVER_ROLES)),
               db: Session = Depends(get_db), runtime: PlatformRuntime = Depends(get_runtime)) -> BankValidationOut:
    """Validate-only preview (creates nothing) so the verify dialog can show the result before confirming."""
    emp, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    return BankValidationOut(**validate_bank(runtime, emp, c))


# ------------------------------------------------------------------ onboarding links (staff side)
class OnboardingLinkOut(BaseModel):
    id: uuid.UUID
    invite_path: str | None = None  # returned ONCE, at creation. The token itself is never stored.
    email: str | None
    status: str
    expires_at: datetime
    created_at: datetime


def _invite_out(inv: ContractorOnboardingInvite, raw: str | None = None) -> OnboardingLinkOut:
    return OnboardingLinkOut(id=inv.id, invite_path=f"/onboard/{raw}" if raw else None, email=inv.email,
                             status=_invite_status(inv), expires_at=inv.expires_at, created_at=inv.created_at)


def _invite_status(inv: ContractorOnboardingInvite) -> str:
    if inv.status == "PENDING" and aware(inv.expires_at) <= datetime.now(UTC):
        return "EXPIRED"
    return inv.status


@router.post("/employers/{employer_id}/contractors/{contractor_id}/onboarding-link",
             response_model=OnboardingLinkOut, status_code=status.HTTP_201_CREATED)
def create_onboarding_link(employer_id: uuid.UUID, contractor_id: uuid.UUID, request: Request,
                           tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                           db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> OnboardingLinkOut:
    emp, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    for old in db.scalars(select(ContractorOnboardingInvite).where(
            ContractorOnboardingInvite.contractor_id == c.id, ContractorOnboardingInvite.status == "PENDING")):
        old.status = "REVOKED"  # one live link per contractor
    token = secrets.token_urlsafe(32)
    inv = ContractorOnboardingInvite(
        organization_id=tenant.organization.id, employer_id=emp.id, contractor_id=c.id, email=c.email,
        token_hash=_hash(token), status="PENDING", expires_at=datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS),
        created_by_id=tenant.user.id)
    db.add(inv)
    _log(db, c, "onboarding", f"Onboarding link created (valid {INVITE_TTL_DAYS} days)", tenant.user.id)
    db.commit()
    if trust_notify.onboarding_link(db, settings, tenant.organization, emp, c, invite_id=inv.id, token=token,
                                    days=INVITE_TTL_DAYS):
        notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return _invite_out(inv, token)


@router.post("/employers/{employer_id}/contractors/{contractor_id}/onboarding-link/{invite_id}/revoke",
             response_model=OnboardingLinkOut)
def revoke_onboarding_link(employer_id: uuid.UUID, contractor_id: uuid.UUID, invite_id: uuid.UUID,
                           tenant: Tenant = Depends(require_roles(EDITOR_ROLES)),
                           db: Session = Depends(get_db)) -> OnboardingLinkOut:
    _, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    inv = db.scalar(select(ContractorOnboardingInvite).where(
        ContractorOnboardingInvite.id == invite_id, ContractorOnboardingInvite.contractor_id == c.id,
        ContractorOnboardingInvite.organization_id == tenant.organization.id))
    if inv is None or inv.status != "PENDING":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Onboarding link not found.")
    inv.status = "REVOKED"
    _log(db, c, "onboarding", "Onboarding link revoked", tenant.user.id)
    db.commit()
    return _invite_out(inv)


# ------------------------------------------------------------------ onboarding (public)
def _throttle(request: Request) -> tuple[LoginThrottle, str]:
    throttle: LoginThrottle = request.app.state.onboarding_throttle
    key = f"onboard|{request.client.host if request.client else '-'}"
    wait = throttle.retry_after(key)
    if wait:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    return throttle, key


def _live_invite(db: Session, token: str, throttle: LoginThrottle, key: str) -> ContractorOnboardingInvite:
    """One uniform 404 for unknown / expired / revoked / used links (never reveals which)."""
    digest = _hash(token)
    inv = db.scalar(select(ContractorOnboardingInvite).where(ContractorOnboardingInvite.token_hash == digest))
    if (inv is None or not hmac.compare_digest(inv.token_hash, digest) or inv.status != "PENDING"
            or aware(inv.expires_at) <= datetime.now(UTC)):
        throttle.record_failure(key)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is not valid any more. Ask your agency for a new one.")
    return inv


class PayoutOptionOut(BaseModel):
    key: str
    currency: str
    method: str
    label: str
    fields: list[str]


class PublicOnboardingOut(BaseModel):
    contractor_name: str
    first_name: str
    employer_name: str
    agency_name: str
    country: str
    payout_options: list[PayoutOptionOut]
    expires_at: datetime


@router.get("/onboarding/{token}", response_model=PublicOnboardingOut)
def public_onboarding(token: str, request: Request, db: Session = Depends(get_db)) -> PublicOnboardingOut:
    throttle, key = _throttle(request)
    inv = _live_invite(db, token, throttle, key)
    c, emp, org = db.get(Contractor, inv.contractor_id), db.get(Employer, inv.employer_id), db.get(Organization, inv.organization_id)
    return PublicOnboardingOut(
        contractor_name=c.full_name, first_name=c.full_name.split()[0], employer_name=emp.name, agency_name=org.name,
        country=c.country, payout_options=[PayoutOptionOut(**o) for o in PAYOUT_OPTIONS], expires_at=inv.expires_at)


class OnboardingSubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option: Literal["EUR_SWIFT", "EUR_SEPA", "USD_ACH", "GBP_FPS"]
    country: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    iban: str | None = Field(default=None, max_length=40)
    swift_code: str | None = Field(default=None, max_length=16)
    account_number: str | None = Field(default=None, max_length=40)
    routing_code: str | None = Field(default=None, max_length=24)
    tax_status: Literal["US_PERSON", "NON_US"]  # self-declaration only: NO tax ids, NO documents
    confirmed_accurate: bool

    @field_validator("confirmed_accurate")
    @classmethod
    def _confirmed(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("Please confirm the details are correct.")
        return v


class OnboardingSubmitOut(BaseModel):
    status: str
    message: str


def _digits(value: str | None) -> str | None:
    return "".join(ch for ch in value if ch.isalnum()) if value else None


@router.post("/onboarding/{token}/submit", response_model=OnboardingSubmitOut)
def submit_onboarding(token: str, body: OnboardingSubmitIn, request: Request, db: Session = Depends(get_db),
                      runtime: PlatformRuntime = Depends(get_runtime)) -> OnboardingSubmitOut:
    throttle, key = _throttle(request)
    inv = _live_invite(db, token, throttle, key)
    c, emp = db.get(Contractor, inv.contractor_id), db.get(Employer, inv.employer_id)
    option = next(o for o in PAYOUT_OPTIONS if o["key"] == body.option)
    fields = {f: (getattr(body, f) or None) for f in ("iban", "swift_code", "account_number", "routing_code")}
    for f in ("account_number", "routing_code"):
        fields[f] = _digits(fields[f])
    for f in ("iban", "swift_code"):
        fields[f] = fields[f].replace(" ", "").upper() if fields[f] else None
    # Only the fields the chosen option uses are kept.
    fields = {f: (v if f in option["fields"] else None) for f, v in fields.items()}
    if body.option == "EUR_SEPA":
        fields["swift_code"] = (body.swift_code or "").replace(" ", "").upper() or None
    problems = []
    if option["currency"] == "USD":
        if not fields["routing_code"] or not (fields["routing_code"].isdigit() and len(fields["routing_code"]) == 9):
            problems.append("routing_code: a US routing number has 9 digits")
        if fields["account_number"] and not (fields["account_number"].isdigit() and 4 <= len(fields["account_number"]) <= 17):
            problems.append("account_number: a US account number has 4 to 17 digits")
    if option["currency"] == "GBP":
        if not fields["routing_code"] or not (fields["routing_code"].isdigit() and len(fields["routing_code"]) == 6):
            problems.append("routing_code: a UK sort code has 6 digits")
        if fields["account_number"] and not (fields["account_number"].isdigit() and len(fields["account_number"]) == 8):
            problems.append("account_number: a UK account number has 8 digits")
    try:
        ContractorIn(full_name=c.full_name, email=c.email, country=body.country, currency=option["currency"],
                     payout_method=option["method"], default_amount=str(c.default_amount) if c.default_amount else None,
                     **fields)
    except ValidationError as exc:
        problems.append(_validation_message(exc))
    if problems:
        throttle.record_failure(key)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(problems))

    old_bank = bank_snapshot(c)
    c.country, c.currency, c.payout_method = body.country.upper(), option["currency"], option["method"]
    for f, v in fields.items():
        setattr(c, f, v)
    check = validate_bank(runtime, emp, c)
    if check["checked"] and not check["valid"]:
        # Nothing is saved: restore and let the contractor fix their typo.
        db.rollback()
        throttle.record_failure(key)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Your bank didn't accept these details. Please check them for typos "
                            f"(fields: {', '.join(check['errors']) or 'bank details'}).")
    now = datetime.now(UTC)
    new_bank = bank_snapshot(c)
    c.bank_verification_status = "PENDING_VERIFICATION"  # the portal never makes details payable
    c.bank_details_changed_at, c.onboarded_at = now, now
    c.bank_change_requested_by_id = None  # submitted by the contractor: any permitted staff may verify
    c.bank_verified_by_id, c.bank_verified_at = None, None
    c.tax_status = body.tax_status
    if c.tax_form_status != "RECEIVED":
        c.tax_form_type = "W9" if body.tax_status == "US_PERSON" else "W8BEN"
    inv.status, inv.accepted_at = "ACCEPTED", now
    changes = bank_change_summary(old_bank, new_bank) or "no change to stored bank details"
    _log(db, c, "onboarding",
         f"Contractor submitted details via onboarding link: {changes}. Self-declared: "
         f"{'US person' if body.tax_status == 'US_PERSON' else 'non-US person'}. Awaiting staff verification.", None)
    db.commit()
    settings = request.app.state.settings
    org = db.get(Organization, c.organization_id)
    if org is not None and trust_notify.bank_change_callback_request(db, settings, org, emp, c):
        notify.dispatch_async(request.app.state.db.sessionmaker, settings)
    return OnboardingSubmitOut(status="PENDING_VERIFICATION",
                               message="Your details were sent. Your agency will confirm them before your next payment.")


# ------------------------------------------------------------------ history
class ChangeOut(BaseModel):
    id: uuid.UUID
    group: str
    summary: str
    changed_by: str | None
    created_at: datetime


class PaymentOut(BaseModel):
    run_id: uuid.UUID
    period_label: str | None
    amount: str
    currency: str
    status: str
    created_at: datetime


class HistoryOut(BaseModel):
    changes: list[ChangeOut]
    payments: list[PaymentOut]
    invites: list[OnboardingLinkOut]


@router.get("/employers/{employer_id}/contractors/{contractor_id}/history", response_model=HistoryOut)
def contractor_history(employer_id: uuid.UUID, contractor_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                       db: Session = Depends(get_db)) -> HistoryOut:
    _, c = _contractor_or_404(db, tenant, employer_id, contractor_id)
    changes = db.execute(
        select(ContractorChange, User.full_name, User.email).outerjoin(User, User.id == ContractorChange.changed_by_id)
        .where(ContractorChange.contractor_id == c.id, ContractorChange.organization_id == tenant.organization.id)
        .order_by(ContractorChange.created_at.desc()).limit(100)).all()
    payments = db.execute(
        select(PayLine, PayrollRun).join(PayrollRun, PayLine.run_id == PayrollRun.id)
        .where(PayLine.contractor_id == c.id, PayrollRun.organization_id == tenant.organization.id)
        .order_by(PayrollRun.created_at.desc()).limit(100)).all()
    invites = db.scalars(select(ContractorOnboardingInvite).where(
        ContractorOnboardingInvite.contractor_id == c.id, ContractorOnboardingInvite.organization_id == tenant.organization.id)
        .order_by(ContractorOnboardingInvite.created_at.desc()).limit(10)).all()
    return HistoryOut(
        changes=[ChangeOut(id=ch.id, group=ch.change_group, summary=ch.summary,
                           changed_by=(name or email) if ch.changed_by_id else "Contractor (onboarding link)",
                           created_at=ch.created_at) for ch, name, email in changes],
        payments=[PaymentOut(run_id=r.id, period_label=r.period_label, amount=money_str(p.amount), currency=p.currency,
                             status=p.status, created_at=r.created_at) for p, r in payments],
        invites=[_invite_out(i) for i in invites])


# ------------------------------------------------------------------ compliance overview
def _compliance(db: Session, tenant: Tenant, settings: Settings) -> dict:
    today = today_utc()
    org_id = tenant.organization.id
    threshold = settings.tax_1099_threshold_usd
    ytd = ytd_paid(db, org_id, today.year)
    rows = db.execute(
        select(Contractor, Employer.name).join(Employer, Employer.id == Contractor.employer_id)
        .where(Contractor.organization_id == org_id, Contractor.is_active.is_(True))
        .order_by(Employer.name, Contractor.full_name)).all()
    requesters = {u.id: (u.full_name or u.email) for u in db.scalars(
        select(User).where(User.id.in_({c.bank_change_requested_by_id for c, _ in rows if c.bank_change_requested_by_id})))}
    tax_forms, bank_pending, watch, ytd_rows = [], [], [], []
    for c, employer_name in rows:
        base = {"contractor_id": str(c.id), "employer_id": str(c.employer_id), "employer_name": employer_name,
                "full_name": c.full_name, "country": c.country}
        form_status = effective_tax_form_status(c, today)
        issue, days_left = None, None
        if form_status == "EXPIRED":
            issue = "expired"
        elif form_status == "MISSING":
            issue = "missing"
        elif c.tax_form_expires_at is not None:
            days_left = (c.tax_form_expires_at - today).days
            if days_left <= TAX_EXPIRY_WARNING_DAYS:
                issue = "expiring_soon"
        if c.tax_status == "UNKNOWN" and issue is None:
            issue = "status_unknown"
        if issue:
            tax_forms.append({**base, "issue": issue, "tax_status": c.tax_status, "tax_form_type": c.tax_form_type,
                              "tax_form_status": form_status, "tax_form_expires_at": c.tax_form_expires_at,
                              "days_to_expiry": days_left})
        if c.bank_verification_status == "PENDING_VERIFICATION":
            changed = aware(c.bank_details_changed_at)
            bank_pending.append({**base, "changed_at": changed,
                                 "age_hours": int((datetime.now(UTC) - changed).total_seconds() // 3600) if changed else None,
                                 "requested_by_id": str(c.bank_change_requested_by_id) if c.bank_change_requested_by_id else None,
                                 "requested_by": requesters.get(c.bank_change_requested_by_id, "Contractor (onboarding link)"
                                                                if c.onboarded_at else None)})
        y = ytd.get(c.id)
        usd = y.usd if y else Decimal("0.00")
        over = over_1099_threshold(c, usd, threshold)
        ytd_row = {**base, "tax_status": c.tax_status, "tax_form_status": form_status, "tax_form_type": c.tax_form_type,
                   "paid_ytd_usd": money_str(usd), "payments": y.payments if y else 0,
                   "paid_by_currency": {k: money_str(v) for k, v in (y.by_currency if y else {}).items()},
                   "unpriced_lines": y.unpriced_lines if y else 0, "over_1099_threshold": over}
        ytd_rows.append(ytd_row)
        if over:
            watch.append({**ytd_row, "w9_on_file": form_status == "RECEIVED" and c.tax_form_type == "W9"})
    ytd_rows.sort(key=lambda r: Decimal(r["paid_ytd_usd"]), reverse=True)
    watch.sort(key=lambda r: Decimal(r["paid_ytd_usd"]), reverse=True)
    return {"year": today.year, "threshold_usd": money_str(threshold), "disclaimer": NOT_TAX_ADVICE,
            "counts": {"tax_forms": len(tax_forms), "bank_pending": len(bank_pending), "watch_1099": len(watch),
                       "contractors": len(rows)},
            "tax_forms": tax_forms, "bank_pending": bank_pending, "watch_1099": watch, "ytd": ytd_rows}


@router.get("/compliance/overview")
def compliance_overview(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                        settings: Settings = Depends(get_settings)) -> dict:
    return _compliance(db, tenant, settings)


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text  # spreadsheet formula injection


@router.get("/compliance/ytd.csv")
def compliance_ytd_csv(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)) -> Response:
    data = _compliance(db, tenant, settings)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["employer", "contractor", "country", "tax_status", "tax_form_type", "tax_form_status",
                f"paid_ytd_usd_{data['year']}", "payments", "paid_by_currency", "over_1099_threshold"])
    for r in data["ytd"]:
        w.writerow([_csv_cell(r["employer_name"]), _csv_cell(r["full_name"]), r["country"], r["tax_status"],
                    r["tax_form_type"], r["tax_form_status"], r["paid_ytd_usd"], r["payments"],
                    "; ".join(f"{k} {v}" for k, v in r["paid_by_currency"].items()),
                    "yes" if r["over_1099_threshold"] else "no"])
    w.writerow([])
    w.writerow([_csv_cell(f"USD equivalents use each run's stored pricing rate. Threshold USD {data['threshold_usd']}. {NOT_TAX_ADVICE}")])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="paysilo-ytd-{data["year"]}.csv"'})
