"""Request/response models. Money is always a decimal string on the wire."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

CountryCode = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
CurrencyCode = Field(min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")


def _money(value: object) -> str:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("must be a decimal amount") from None
    if not d.is_finite() or d <= 0:
        raise ValueError("must be a positive amount")
    if d.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("at most 2 decimal places")
    if d > Decimal("100000000"):
        raise ValueError("amount is unreasonably large")
    return format(d.quantize(Decimal("0.01")), "f")


# ---------------------------------------------------------------- auth
class OrganizationIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    legal_name: str | None = Field(default=None, max_length=200)
    country: str = CountryCode
    base_currency: str = CurrencyCode
    industry: str | None = Field(default=None, max_length=80)
    size_band: Literal["1-10", "11-50", "51-200", "201-1000", "1000+"] | None = None
    website: str | None = Field(default=None, max_length=200)

    @field_validator("country", "base_currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("name", "legal_name", "industry", "website")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        v = v.strip() if isinstance(v, str) else v
        return v or None


class AdminIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _strong_enough(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("must be at most 72 bytes")
        if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
            raise ValueError("must contain at least one letter and one number")
        return v


class SignupIn(BaseModel):
    organization: OrganizationIn
    admin: AdminIn


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)

    @field_validator("full_name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("must contain at least 2 characters")
        return value


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("must be at most 72 bytes")
        if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
            raise ValueError("must contain at least one letter and one number")
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    full_name: str


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    slug: str
    legal_name: str | None
    country: str
    base_currency: str
    industry: str | None
    size_band: str | None
    website: str | None
    is_demo: bool


class SessionOut(BaseModel):
    user: UserOut
    organization: OrganizationOut
    role: str


class DemoCredentialsOut(BaseModel):
    email: str
    password: str
    organization: str


# ---------------------------------------------------------------- employers
class EmployerIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    legal_name: str | None = Field(default=None, max_length=200)
    country: str = CountryCode
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None
    funding_currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("country", "funding_currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("funding_currency")
    @classmethod
    def _usd_only(cls, v: str) -> str:
        if v != "USD":
            raise ValueError("employer wallets are funded in USD in this release")
        return v


class ContractorIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr | None = None
    country: str = CountryCode
    currency: str = CurrencyCode
    payout_method: Literal["SWIFT", "LOCAL"] = "SWIFT"
    iban: str | None = Field(default=None, min_length=15, max_length=34)
    swift_code: str | None = Field(default=None, min_length=8, max_length=11)
    account_number: str | None = Field(default=None, min_length=4, max_length=34)
    routing_code: str | None = Field(default=None, max_length=20)
    default_amount: str | None = None

    @field_validator("country", "currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("iban", "swift_code")
    @classmethod
    def _compact(cls, v: str | None) -> str | None:
        return v.replace(" ", "").upper() if v else None

    @field_validator("default_amount")
    @classmethod
    def _amount(cls, v: str | None) -> str | None:
        return _money(v) if v not in (None, "") else None

    @model_validator(mode="after")
    def _bank_details(self) -> "ContractorIn":
        if not (self.iban or self.account_number):
            raise ValueError("provide an IBAN or an account number")
        if self.payout_method == "SWIFT" and not self.swift_code:
            raise ValueError("SWIFT payouts need a SWIFT/BIC code")
        return self


class ContractorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    full_name: str
    email: str | None
    country: str
    currency: str
    payout_method: str
    iban_last4: str | None = None
    account_last4: str | None = None
    swift_code: str | None
    default_amount: str | None
    is_active: bool
    # contractor trust (status/dates only - never tax ids, never full bank values)
    tax_status: str = "UNKNOWN"
    tax_form_type: str = "NONE"
    tax_form_status: str = "MISSING"  # MISSING|RECEIVED|EXPIRED (EXPIRED computed on read)
    tax_form_received_at: date | None = None
    tax_form_expires_at: date | None = None
    bank_verification_status: str = "VERIFIED"
    bank_details_changed_at: datetime | None = None
    bank_change_requested_by_id: uuid.UUID | None = None
    bank_verified_at: datetime | None = None
    onboarded_at: datetime | None = None


class EmployerOut(BaseModel):
    id: uuid.UUID
    name: str
    legal_name: str | None
    country: str
    contact_name: str | None
    contact_email: str | None
    funding_currency: str
    status: str
    airwallex_account_id: str | None
    balances: dict[str, str]
    contractor_count: int
    created_at: datetime


class EmployerDetailOut(EmployerOut):
    contractors: list[ContractorOut]


class FundIn(BaseModel):
    amount: str
    reference: str | None = Field(default=None, max_length=60)

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        return _money(v)


# ---------------------------------------------------------------- runs
class RunLineIn(BaseModel):
    contractor_id: uuid.UUID
    amount: str | None = None  # defaults to the contractor's default_amount

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str | None) -> str | None:
        return _money(v) if v not in (None, "") else None


class RunIn(BaseModel):
    employer_id: uuid.UUID
    lines: list[RunLineIn] | None = Field(default=None, max_length=500)
    instruction: str | None = Field(default=None, max_length=20000)  # free text for the Intake Agent
    period_label: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _one_source(self) -> "RunIn":
        if bool(self.lines) == bool(self.instruction and self.instruction.strip()):
            raise ValueError("provide either lines or an instruction, not both")
        return self


class DecisionIn(BaseModel):
    decision: Literal["approve", "hold"]


class PayLineOut(BaseModel):
    id: str
    position: int
    beneficiary_name: str
    amount: str
    currency: str
    method: str
    payout_fee: str | None
    status: str
    airwallex_transfer_id: str | None


class EventOut(BaseModel):
    seq: int
    agent: str
    level: str
    message: str
    created_at: datetime


class ApprovalOut(BaseModel):
    kind: str
    decision: str
    decided_by_email: str
    verified: bool
    tap_key_id: str | None
    tap_signature: str | None
    created_at: datetime


class ReceiptOut(BaseModel):
    chain: str
    chain_index: int
    receipt_hash: str
    payload_hash: str
    tx_hash: str | None
    explorer_url: str | None = None


# ---------------------------------------------------------------- Visa TAP
class TapKeyOut(BaseModel):
    keyid: str
    agent_id: str
    algorithm: str
    status: str  # active | retired
    public_key: str  # raw Ed25519 public key, base64
    public_key_pem: str
    created_at: str | None
    retired_at: str | None
    expires_at: str | None


class TapKeysOut(BaseModel):
    keys: list[TapKeyOut]


class TapVerifyIn(BaseModel):
    approval_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "TapVerifyIn":
        if (self.approval_id is None) == (self.run_id is None):
            raise ValueError("provide exactly one of approval_id or run_id")
        return self


class TapVerifyOut(BaseModel):
    approval_id: str
    run_id: str
    verified: bool
    keyid: str | None
    agent_id: str | None
    code: str
    reason: str
    decided_by_email: str
    decision: str


class RunOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    status: str
    source: str
    parent_run_id: uuid.UUID | None = None
    period_label: str | None
    payroll_currency: str | None
    required_usd: str | None
    shortfall_usd: str | None
    fee_charged_usd: str
    explanation: str | None
    error: str | None
    pending: dict | None
    resumable: bool
    created_at: datetime
    settled_at: datetime | None


class RunDetailOut(RunOut):
    assessment: dict | None
    pay_lines: list[PayLineOut]
    events: list[EventOut]
    approvals: list[ApprovalOut]
    receipt: ReceiptOut | None
    continuation_run_id: uuid.UUID | None = None  # newest "pay held lines" run that continues this one
    held_line_count: int = 0
    can_pay_held: bool = False  # held lines can be paid right now (role-independent)
    run_fee_collected: bool = False  # the run fee was collected on this run or a parent up the chain


class OverviewOut(BaseModel):
    organization: OrganizationOut
    employers: list[EmployerOut]
    platform_fees: dict[str, str]
    runs_total: int
    runs_settled: int
    runs_waiting: int
    mode: str


# ---------------------------------------------------------------- benefits (Visa VIC)
class VicStatusOut(BaseModel):
    mode: str
    credential_ref: str
    test_card: bool
    tx_limit_usd: str | None
    cli_available: bool
    authenticated: bool = True
    two_phase: bool = False


class BenefitBudgetOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    currency: str
    monthly_cap: str
    per_tx_limit: str | None
    spent_this_period: str
    remaining: str
    period: str
    enabled: bool
    locked: bool
    lock_reason: str | None


class BenefitBudgetIn(BaseModel):
    currency: str = CurrencyCode
    monthly_cap: str
    per_tx_limit: str | None = None
    enabled: bool

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("monthly_cap")
    @classmethod
    def _cap(cls, v: str) -> str:
        return _money(v)

    @field_validator("per_tx_limit")
    @classmethod
    def _limit(cls, v: str | None) -> str | None:
        return _money(v) if v not in (None, "") else None


class BenefitRequestIn(BaseModel):
    employer_id: uuid.UUID
    contractor_id: uuid.UUID | None = None
    contractor_name: str | None = Field(default=None, max_length=120)
    merchant: str = Field(min_length=1, max_length=200)
    item: str = Field(min_length=1, max_length=300)
    amount: str
    currency: str = CurrencyCode

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("merchant", "item", "contractor_name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        v = v.strip() if isinstance(v, str) else v
        return v or None

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        return _money(v)


class BenefitPurchaseOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    employer_id: uuid.UUID
    attempt_no: int
    mode: str
    credential_ref: str
    provider_ref: str | None
    status: str
    failure_code: str | None
    failure_detail: str | None
    amount: str
    currency: str
    journal_txn_id: int | None
    created_at: datetime
    provider_mandate_id: str | None = None
    approval_url: str | None = None
    mandate_status: str | None = None


class BenefitCompleteIn(BaseModel):
    purchase_confirmed: bool = False


class BenefitRequestOut(BaseModel):
    id: uuid.UUID
    employer_id: uuid.UUID
    employer_name: str
    contractor_id: str | None
    contractor_name: str
    merchant: str
    item: str
    amount: str
    currency: str
    status: str
    requested_by_email: str
    decided_by_email: str | None
    decided_at: str | None
    deny_reason: str | None
    approval_expires_at: str | None
    approval_hash: str | None
    created_at: datetime
    latest_purchase: BenefitPurchaseOut | None


class BenefitDecisionIn(BaseModel):
    decision: Literal["approve", "deny"]
    reason: str | None = Field(default=None, max_length=2000)
