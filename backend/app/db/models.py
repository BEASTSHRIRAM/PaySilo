"""PaySilo data model (SQLAlchemy 2, Postgres/Supabase-first, SQLite-compatible).

Tenancy has two levels and both are explicit on every row that matters:

    Organization (the payroll agency, e.g. Vertex Payroll Services)
      └── Employer (a client company = one Airwallex connected account = one silo)
            ├── Contractor (a person the employer pays = one beneficiary)
            └── PayrollRun ─┬─ PayLine (one payout)
                            ├─ Approval (Visa TAP-signed human decision)
                            ├─ AgentEvent (what each agent did, in order)
                            ├─ AirwallexRequest (every API call, keyed by request_id)
                            └─ SettlementReceipt (Metal anchor of the reconciliation)
    LedgerTransaction ─ LedgerEntry (double-entry, one employer per transaction)

Money is ``NUMERIC(19, 2)``; balances are never stored, only derived from
ledger entries. ``organization_id`` is denormalised onto child rows so every
query can (and does) filter by tenant without a join.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(19, 2)
# none_as_null: Python None is stored as SQL NULL (so "IS NULL" filters work), not JSON null.
JsonType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
# SQLite only auto-increments INTEGER PRIMARY KEY, so BigInteger ids fall back to Integer there.
BigId = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


ROLES = ("owner", "admin", "approver", "viewer")
EMPLOYER_STATUSES = ("PENDING", "ACTIVE", "SUSPENDED")
PAYOUT_METHODS = ("SWIFT", "LOCAL")
RUN_STATUSES = ("RECEIVED", "PRICED", "RESERVED", "APPROVED", "SENT", "SETTLED", "HELD_SHORTFALL",
                "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED", "FAILED",
                # v3: partial payout + post-payout failures (money already moved)
                "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING")
PAY_LINE_STATUSES = ("PENDING", "PAID", "HELD", "FAILED")
APPROVAL_KINDS = ("approval", "escalation")
DECISIONS = ("approve", "hold")
INVITATION_STATUSES = ("PENDING", "ACCEPTED", "REVOKED")
PAY_FREQUENCIES = ("WEEKLY", "BIWEEKLY", "MONTHLY")
NOTIFICATION_KINDS = ("FUNDING_REMINDER", "FUNDING_STATEMENT", "FUNDING_OVERDUE", "FUNDS_RECEIVED",
                      "APPROVAL_REQUESTED", "HELD_LINES_REMINDER", "RUN_SETTLED", "PAYMENT_FAILED", "INVITE",
                      "PAYMENT_NOTICE", "GENERIC")
NOTIFICATION_STATUSES = ("QUEUED", "SENT", "FAILED")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Identity & tenancy
# ---------------------------------------------------------------------------

class Organization(TimestampMixin, Base):
    """The platform customer: a payroll agency running payroll for many employers."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), nullable=False)  # ISO 3166-1 alpha-2
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    industry: Mapped[str | None] = mapped_column(String(80))
    size_band: Mapped[str | None] = mapped_column(String(20))  # "1-10", "11-50", ...
    website: Mapped[str | None] = mapped_column(String(200))
    airwallex_platform_account_id: Mapped[str | None] = mapped_column(String(64))
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    memberships: Mapped[list[Membership]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    employers: Mapped[list[Employer]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)  # stored lower-case
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),
        CheckConstraint(_in("role", ROLES), name="ck_membership_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Invitation(TimestampMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(_in("role", ("admin", "approver", "viewer")), name="ck_invitation_role"),
        CheckConstraint(_in("status", INVITATION_STATUSES), name="ck_invitation_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Notification(TimestampMixin, Base):
    """Outbox row: one message PaySilo wants delivered (funding reminders, statements...).

    ``dedupe_key`` makes queueing idempotent per organization (a nullable key is never deduplicated).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("organization_id", "dedupe_key", name="uq_notification_org_dedupe"),
        CheckConstraint(_in("status", NOTIFICATION_STATUSES), name="ck_notification_status"),
        CheckConstraint(_in("kind", NOTIFICATION_KINDS), name="ck_notification_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employers.id", ondelete="SET NULL"))
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contractors.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERIC")
    channel: Mapped[str] = mapped_column(String(12), nullable=False, default="email")
    recipient: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="QUEUED")
    provider: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)  # the last delivery error (never contains credentials)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    meta: Mapped[dict | None] = mapped_column(JsonType)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Employers (silos) & contractors
# ---------------------------------------------------------------------------

class Employer(TimestampMixin, Base):
    """One client company. One Airwallex connected account. One silo."""

    __tablename__ = "employers"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_employer_org_name"),
        CheckConstraint(_in("status", EMPLOYER_STATUSES), name="ck_employer_status"),
        CheckConstraint("pay_frequency IS NULL OR " + _in("pay_frequency", PAY_FREQUENCIES),
                        name="ck_employer_pay_frequency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    funding_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    airwallex_account_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # The employer's own funding (global) account; simulated deposits land here.
    airwallex_global_account_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    # Funding planner: when this employer's payroll is paid and how many business days
    # before that its wallet must already hold the money.
    pay_frequency: Mapped[str | None] = mapped_column(String(12))
    next_pay_date: Mapped[date | None] = mapped_column(Date)
    funding_lead_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")

    organization: Mapped[Organization] = relationship(back_populates="employers")
    contractors: Mapped[list[Contractor]] = relationship(back_populates="employer", cascade="all, delete-orphan",
                                                         order_by="Contractor.created_at")

    @property
    def ledger_key(self) -> str:
        """The employer's ledger namespace — globally unique, never a display name."""
        return f"emp_{self.id.hex}"


class Contractor(TimestampMixin, Base):
    """A person an employer pays. Maps 1:1 to an Airwallex beneficiary."""

    __tablename__ = "contractors"
    __table_args__ = (
        CheckConstraint(_in("payout_method", PAYOUT_METHODS), name="ck_contractor_method"),
        CheckConstraint("iban IS NOT NULL OR account_number IS NOT NULL", name="ck_contractor_bank_details"),
        CheckConstraint("default_amount IS NULL OR default_amount > 0", name="ck_contractor_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payout_method: Mapped[str] = mapped_column(String(8), nullable=False, default="SWIFT")
    iban: Mapped[str | None] = mapped_column(String(34))
    swift_code: Mapped[str | None] = mapped_column(String(11))
    account_number: Mapped[str | None] = mapped_column(String(34))
    routing_code: Mapped[str | None] = mapped_column(String(20))
    default_amount: Mapped[Decimal | None] = mapped_column(Money)
    airwallex_beneficiary_id: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- contractor trust: tax paperwork status (never tax ids or documents) ---
    tax_status: Mapped[str] = mapped_column(String(10), nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    tax_form_type: Mapped[str] = mapped_column(String(8), nullable=False, default="NONE", server_default="NONE")
    tax_form_status: Mapped[str] = mapped_column(String(10), nullable=False, default="MISSING", server_default="MISSING")
    tax_form_received_at: Mapped[date | None] = mapped_column(Date)
    tax_form_expires_at: Mapped[date | None] = mapped_column(Date)
    # --- bank-detail change control (four-eyes) ---
    # VERIFIED = payable. PENDING_VERIFICATION = bank details changed/submitted and not yet
    # confirmed by a second person after an independent callback.
    bank_verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="VERIFIED", server_default="VERIFIED")
    bank_details_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_change_requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    bank_verified_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    bank_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employer: Mapped[Employer] = relationship(back_populates="contractors")


class ContractorChange(Base):
    """Audit trail of contractor edits. ``summary`` is MASKED text (last 4 only), never full bank values."""

    __tablename__ = "contractor_changes"
    __table_args__ = (
        CheckConstraint(_in("change_group", ("bank", "profile", "tax", "onboarding")), name="ck_contractor_change_group"),
        Index("ix_contractor_changes_contractor", "contractor_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    contractor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contractors.id", ondelete="CASCADE"), nullable=False)
    change_group: Mapped[str] = mapped_column(String(12), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ContractorOnboardingInvite(Base):
    """A one-time self-serve onboarding link. Only the sha256 of the token is stored."""

    __tablename__ = "contractor_onboarding_invites"
    __table_args__ = (
        CheckConstraint(_in("status", INVITATION_STATUSES), name="ck_onboarding_invite_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False)
    contractor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contractors.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Payroll runs
# ---------------------------------------------------------------------------

class PayrollRun(TimestampMixin, Base):
    """One employer's payroll run. ``id`` is also the LangGraph thread id."""

    __tablename__ = "payroll_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_run_status"),
        Index("ix_runs_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # "Pay held lines": a continuation run points at the run whose HELD lines it pays.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_runs.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")  # manual|text|chat|demo|continuation
    period_label: Mapped[str | None] = mapped_column(String(60))
    pay_date: Mapped[date | None] = mapped_column(Date)
    raw_input: Mapped[str | None] = mapped_column(Text)
    payroll_currency: Mapped[str | None] = mapped_column(String(5))  # ISO code or "MIXED"
    required_usd: Mapped[Decimal | None] = mapped_column(Money)
    shortfall_usd: Mapped[Decimal | None] = mapped_column(Money)
    fee_charged_usd: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.00"))
    assessment: Mapped[dict | None] = mapped_column(JsonType)
    pending_interrupt: Mapped[dict | None] = mapped_column(JsonType)
    explanation: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    employer: Mapped[Employer] = relationship()
    pay_lines: Mapped[list[PayLine]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                    order_by="PayLine.position")
    events: Mapped[list[AgentEvent]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                    order_by="AgentEvent.seq")
    approvals: Mapped[list[Approval]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                     order_by="Approval.created_at")


class PayLine(Base):
    __tablename__ = "pay_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_pay_line_position"),
        CheckConstraint("amount > 0", name="ck_pay_line_amount_positive"),
        CheckConstraint(_in("method", PAYOUT_METHODS), name="ck_pay_line_method"),
        CheckConstraint(_in("status", PAY_LINE_STATUSES), name="ck_pay_line_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False)
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contractors.id", ondelete="SET NULL"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    beneficiary_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="SWIFT")
    payout_fee: Mapped[Decimal | None] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # Airwallex idempotency key
    airwallex_transfer_id: Mapped[str | None] = mapped_column(String(64))

    run: Mapped[PayrollRun] = relationship(back_populates="pay_lines")


class Approval(TimestampMixin, Base):
    """A human decision, with the Visa TAP signature that makes it count."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(_in("kind", APPROVAL_KINDS), name="ck_approval_kind"),
        CheckConstraint(_in("decision", DECISIONS), name="ck_approval_decision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False,
                                              index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_email: Mapped[str] = mapped_column(String(254), nullable=False)
    signed_body: Mapped[str | None] = mapped_column(Text)  # canonical JSON that was signed
    tap_key_id: Mapped[str | None] = mapped_column(String(80))
    tap_signature_input: Mapped[str | None] = mapped_column(Text)
    tap_signature: Mapped[str | None] = mapped_column(Text)
    tap_nonce: Mapped[str | None] = mapped_column(String(64), unique=True)  # single-use, DB-enforced
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[PayrollRun] = relationship(back_populates="approvals")


class AgentEvent(TimestampMixin, Base):
    """Ordered, per-run record of what each agent did. Append-only."""

    __tablename__ = "agent_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_event_seq"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent: Mapped[str] = mapped_column(String(24), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped[PayrollRun] = relationship(back_populates="events")


class AirwallexRequest(TimestampMixin, Base):
    """Every Airwallex call PaySilo made, keyed by its idempotency ``request_id``."""

    __tablename__ = "airwallex_requests"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employers.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    on_behalf_of: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[dict | None] = mapped_column(JsonType)


class SettlementReceipt(TimestampMixin, Base):
    """Metal anchor for a settled run's reconciliation receipt (hashes only)."""

    __tablename__ = "settlement_receipts"
    __table_args__ = (UniqueConstraint("chain", "receipt_hash", name="uq_receipt_chain_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employers.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), nullable=False,
                                              unique=True)
    chain: Mapped[str] = mapped_column(String(16), nullable=False)  # "mock" | "metal-l2"
    chain_index: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(80))  # set when anchored on Metal L2


class TapKey(TimestampMixin, Base):
    """Public half of every Visa TAP agent key PaySilo has signed with.

    Published at ``GET /.well-known/tap-keys``. A key with ``retired_at``
    set no longer signs but stays published and verifiable, so signatures
    made before a rotation keep verifying. A key with ``revoked_at`` set,
    or past ``expires_at``, is no longer published.
    """

    __tablename__ = "tap_keys"

    key_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(80), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(20), nullable=False, default="ed25519")
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Silo chat (per-user, org-scoped conversation memory)
# ---------------------------------------------------------------------------

CHAT_ROLES = ("user", "assistant", "system", "tool")


class ChatThread(TimestampMixin, Base):
    """One Silo chat conversation. Owned by one user inside one organization."""

    __tablename__ = "chat_threads"
    __table_args__ = (Index("ix_chat_threads_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New conversation")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)

    messages: Mapped[list[ChatMessage]] = relationship(back_populates="thread", cascade="all, delete-orphan",
                                                       order_by="ChatMessage.id")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (CheckConstraint(_in("role", CHAT_ROLES), name="ck_chat_message_role"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered thinking/tool trace emitted while this (assistant) message was
    # produced, so the UI can re-render its cards after a reload. NULL for
    # user messages and for assistant messages written before traces existed.
    trace: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    thread: Mapped[ChatThread] = relationship(back_populates="messages")


class ChatSecurityEvent(TimestampMixin, Base):
    """Audit record for refused chat input (cross-tenant / injection attempts)."""

    __tablename__ = "chat_security_events"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_threads.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. "cross_tenant", "instruction_override"
    detail: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Ledger (double-entry, append-only)
# ---------------------------------------------------------------------------

class LedgerTransaction(TimestampMixin, Base):
    """A balanced set of entries touching at most ONE employer namespace."""

    __tablename__ = "ledger_transactions"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employers.id", ondelete="CASCADE"),
                                                          index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="SET NULL"))
    memo: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    entries: Mapped[list[LedgerEntry]] = relationship(back_populates="transaction", cascade="all, delete-orphan")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_ledger_entry_nonzero"),
        Index("ix_ledger_entries_account", "account"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("ledger_transactions.id", ondelete="CASCADE"),
                                                nullable=False, index=True)
    account: Mapped[str] = mapped_column(String(120), nullable=False)  # "<namespace>:<account>:<CCY>"
    namespace: Mapped[str] = mapped_column(String(40), nullable=False)  # employer ledger key | platform | external
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # signed; + = into the account

    transaction: Mapped[LedgerTransaction] = relationship(back_populates="entries")


# ---------------------------------------------------------------------------
# Source imports (CSV payroll obligation imports — guide §8.4 / C04)
# Appended at the END of this module for the v3 bedata lane (additive only).
# ---------------------------------------------------------------------------

IMPORT_STATUSES = ("PREVIEWED", "COMMITTED")
IMPORT_ROW_STATUSES = ("ok", "error", "duplicate")


class SourceImport(TimestampMixin, Base):
    """One CSV file previewed (and optionally committed) for ONE employer.

    Idempotency: ``(employer_id, content_hash)`` is unique — re-previewing
    the same file for the same employer returns the existing import, and a
    second commit returns the run created by the first commit.
    """

    __tablename__ = "source_imports"
    __table_args__ = (
        UniqueConstraint("employer_id", "content_hash", name="uq_source_import_employer_hash"),
        CheckConstraint(_in("status", IMPORT_STATUSES), name="ck_source_import_status"),
        Index("ix_source_imports_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mapping: Mapped[dict | None] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PREVIEWED")
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="SET NULL"))

    rows: Mapped[list[SourceImportRow]] = relationship(back_populates="source_import",
                                                       cascade="all, delete-orphan",
                                                       order_by="SourceImportRow.row_number")


class SourceImportRow(Base):
    __tablename__ = "source_import_rows"
    __table_args__ = (
        CheckConstraint(_in("status", IMPORT_ROW_STATUSES), name="ck_source_import_row_status"),
        Index("ix_source_import_rows_import", "import_id"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_imports.id", ondelete="CASCADE"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ok")
    errors: Mapped[list | None] = mapped_column(JsonType)
    data: Mapped[dict | None] = mapped_column(JsonType)
    business_key: Mapped[str | None] = mapped_column(String(500))

    source_import: Mapped[SourceImport] = relationship(back_populates="rows")


# ---------------------------------------------------------------------------
# Silo Benefits — employer-funded benefits via Visa VIC (guide §6.5,
# invariant 13). Appended at the END of this module (additive only).
# Benefits have their own budget, separate from wage funds and reserves:
# a benefit purchase can never touch an employer's cash accounts.
# ---------------------------------------------------------------------------

BENEFIT_REQUEST_STATUSES = ("REQUESTED", "APPROVED", "DENIED", "PURCHASED", "FAILED")
BENEFIT_PURCHASE_STATUSES = ("SUCCEEDED", "FAILED", "PENDING")
VIC_MODES = ("SIMULATED", "VIC")


class BenefitBudget(TimestampMixin, Base):
    """One employer's benefit budget in one currency.

    Separate from payroll: ``spent_this_period`` is tracked here and in
    ``benefits_*`` ledger accounts only — never against ``cash:``.
    """

    __tablename__ = "benefit_budgets"
    __table_args__ = (
        UniqueConstraint("employer_id", "currency", name="uq_benefit_budget_employer_ccy"),
        CheckConstraint("monthly_cap > 0", name="ck_benefit_budget_cap_positive"),
        CheckConstraint("spent_this_period >= 0", name="ck_benefit_budget_spent_nonneg"),
        CheckConstraint("per_tx_limit IS NULL OR per_tx_limit > 0", name="ck_benefit_budget_per_tx_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    monthly_cap: Mapped[Decimal] = mapped_column(Money, nullable=False)
    per_tx_limit: Mapped[Decimal | None] = mapped_column(Money)
    spent_this_period: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0.00"))
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BenefitRequest(TimestampMixin, Base):
    """One requested benefit purchase, bound to an exact approval snapshot.

    The human approves the exact merchant / item / amount / currency.
    ``approval_hash`` is the hash of that snapshot plus its expiry; any
    change to the snapshot invalidates the approval (checked again at
    purchase time in the benefits service).
    """

    __tablename__ = "benefit_requests"
    __table_args__ = (
        CheckConstraint(_in("status", BENEFIT_REQUEST_STATUSES), name="ck_benefit_request_status"),
        CheckConstraint("amount > 0", name="ck_benefit_request_amount_positive"),
        Index("ix_benefit_requests_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contractors.id", ondelete="SET NULL"))
    contractor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False)
    item: Mapped[str] = mapped_column(String(300), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="REQUESTED")
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_by_email: Mapped[str] = mapped_column(String(254), nullable=False)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_email: Mapped[str | None] = mapped_column(String(254))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deny_reason: Mapped[str | None] = mapped_column(Text)
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)


class BenefitPurchase(TimestampMixin, Base):
    """One purchase attempt for an approved benefit request. Append-only.

    These rows, together with ``decided_by`` / ``decided_at`` on the
    request, are the benefits audit trail (AgentEvent is run-scoped and
    cannot be used here). Only masked credential references are stored —
    never a raw card number.
    """

    __tablename__ = "benefit_purchases"
    __table_args__ = (
        CheckConstraint(_in("status", BENEFIT_PURCHASE_STATUSES), name="ck_benefit_purchase_status"),
        CheckConstraint(_in("mode", VIC_MODES), name="ck_benefit_purchase_mode"),
        CheckConstraint("amount > 0", name="ck_benefit_purchase_amount_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benefit_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)  # SIMULATED | VIC
    credential_ref: Mapped[str] = mapped_column(String(80), nullable=False)  # masked ref only
    provider_ref: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # SUCCEEDED | FAILED
    failure_code: Mapped[str | None] = mapped_column(String(40))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    journal_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_transactions.id", ondelete="SET NULL"))
    # Live Airi mandate (two-phase flow). Never any card data: only the mandate
    # id, the human approval URL and a coarse mandate state / credential receipt.
    provider_mandate_id: Mapped[str | None] = mapped_column(String(80))
    approval_url: Mapped[str | None] = mapped_column(Text)
    mandate_status: Mapped[str | None] = mapped_column(String(40))
