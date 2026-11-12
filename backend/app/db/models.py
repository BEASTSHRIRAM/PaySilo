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
                "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING",
                # PARTIAL settlement_mode (priority walk): some lines settled, the
                # rest HELD and rescuable via a fresh residual plan + approval.
                "PARTIALLY_SETTLED")
SETTLEMENT_MODES = ("ALL_OR_NOTHING", "PARTIAL")
PAY_LINE_STATUSES = ("PENDING", "PAID", "HELD", "FAILED")
APPROVAL_KINDS = ("approval", "escalation")
DECISIONS = ("approve", "hold")
INVITATION_STATUSES = ("PENDING", "ACCEPTED", "REVOKED")


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


# ---------------------------------------------------------------------------
# Employers (silos) & contractors
# ---------------------------------------------------------------------------

class Employer(TimestampMixin, Base):
    """One client company. One Airwallex connected account. One silo."""

    __tablename__ = "employers"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_employer_org_name"),
        CheckConstraint(_in("status", EMPLOYER_STATUSES), name="ck_employer_status"),
        CheckConstraint(_in("settlement_mode", SETTLEMENT_MODES), name="ck_employer_settlement_mode"),
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
    # ALL_OR_NOTHING (default): a short run is held whole. PARTIAL: lines are
    # walked in contractor-priority order and the affordable subset settles.
    settlement_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="ALL_OR_NOTHING")
    airwallex_account_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")

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
    # PARTIAL settlement walk order for this employer (lower pays earlier;
    # 0 = unset, seed order is backfilled 1..n for the demo cast).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    airwallex_beneficiary_id: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    employer: Mapped[Employer] = relationship(back_populates="contractors")


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
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")  # manual|text|demo
    period_label: Mapped[str | None] = mapped_column(String(60))
    pay_date: Mapped[date | None] = mapped_column(Date)
    raw_input: Mapped[str | None] = mapped_column(Text)
    payroll_currency: Mapped[str | None] = mapped_column(String(5))  # ISO code or "MIXED"
    required_usd: Mapped[Decimal | None] = mapped_column(Money)
    shortfall_usd: Mapped[Decimal | None] = mapped_column(Money)
    # PARTIAL runs only: what the HELD lines still need beyond the spendable
    # balance left after phase-1 settlement (see policy.plan_priority_payout).
    residual_shortfall_usd: Mapped[Decimal | None] = mapped_column(Money)
    # The pending/executed residual (phase-2) plan snapshot for a
    # PARTIALLY_SETTLED run: {phase, lines[{pay_line_id, mark, ...}], totals,
    # snapshot_hash, status}. The original approval never covers it — the
    # residual plan needs its own TAP-signed approval (a second Approval row).
    residual_plan: Mapped[dict | None] = mapped_column(JsonType)
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
    # Two-phase history for PARTIAL runs: 1 = settled by the original run
    # execution, 2 = settled by the approved residual (rescue) plan. NULL
    # until the line is PAID. ``settled_at`` is that phase's timestamp.
    settled_phase: Mapped[int | None] = mapped_column(Integer)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
BENEFIT_PURCHASE_STATUSES = ("SUCCEEDED", "FAILED")
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

# ---------------------------------------------------------------------------
# VIC spend categories — Onboarding Kit + Travel & Accommodation.
# Appended at the END of this module (additive only), mirroring Silo
# Benefits: each category has its OWN budget and its OWN journal book
# (onboarding_* / travel_* ledger accounts) and can never touch an
# employer's cash / wage accounts.
# ---------------------------------------------------------------------------

ONBOARDING_KIT_STATUSES = ("DRAFT", "APPROVED", "DENIED", "PARTIALLY_PURCHASED", "COMPLETED")
KIT_ITEM_KINDS = ("BACKGROUND_CHECK", "SAAS_SEAT", "GIFT_CARD", "PERIPHERAL", "CUSTOM")
KIT_ITEM_STATUSES = ("PENDING", "PURCHASED", "FAILED")
KIT_SOURCES = ("MANUAL", "CHAT", "WORKFLOW")
TRIP_STATUSES = ("REQUESTED", "APPROVED", "DENIED", "PARTIALLY_PURCHASED", "COMPLETED")
TRIP_COMPONENT_KINDS = ("TRANSPORT", "LODGING", "PER_DIEM", "OTHER")
TRIP_COMPONENT_STATUSES = ("PENDING", "PURCHASED", "FAILED")
SPEND_PURCHASE_STATUSES = ("SUCCEEDED", "FAILED")


class OnboardingBudget(TimestampMixin, Base):
    """One employer's onboarding budget in one currency (mirrors BenefitBudget)."""

    __tablename__ = "onboarding_budgets"
    __table_args__ = (
        UniqueConstraint("employer_id", "currency", name="uq_onboarding_budget_employer_ccy"),
        CheckConstraint("monthly_cap > 0", name="ck_onboarding_budget_cap_positive"),
        CheckConstraint("spent_this_period >= 0", name="ck_onboarding_budget_spent_nonneg"),
        CheckConstraint("per_tx_limit IS NULL OR per_tx_limit > 0", name="ck_onboarding_budget_per_tx_positive"),
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


class TravelBudget(TimestampMixin, Base):
    """One employer's travel budget in one currency, plus per-trip category caps."""

    __tablename__ = "travel_budgets"
    __table_args__ = (
        UniqueConstraint("employer_id", "currency", name="uq_travel_budget_employer_ccy"),
        CheckConstraint("monthly_cap > 0", name="ck_travel_budget_cap_positive"),
        CheckConstraint("spent_this_period >= 0", name="ck_travel_budget_spent_nonneg"),
        CheckConstraint("per_tx_limit IS NULL OR per_tx_limit > 0", name="ck_travel_budget_per_tx_positive"),
        CheckConstraint("cap_transport > 0 AND cap_lodging > 0 AND cap_per_diem > 0 AND cap_other > 0",
                        name="ck_travel_budget_caps_positive"),
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
    cap_transport: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("750.00"))
    cap_lodging: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("600.00"))
    cap_per_diem: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("300.00"))
    cap_other: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("200.00"))


class OnboardingKit(TimestampMixin, Base):
    """One contractor's onboarding kit: a set of items approved as ONE snapshot."""

    __tablename__ = "onboarding_kits"
    __table_args__ = (
        CheckConstraint(_in("status", ONBOARDING_KIT_STATUSES), name="ck_onboarding_kit_status"),
        CheckConstraint(_in("source", KIT_SOURCES), name="ck_onboarding_kit_source"),
        Index("ix_onboarding_kits_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contractors.id", ondelete="SET NULL"))
    contractor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="MANUAL")
    draft_reason: Mapped[str | None] = mapped_column(Text)
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_email: Mapped[str | None] = mapped_column(String(254))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deny_reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_email: Mapped[str | None] = mapped_column(String(254))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)

    items: Mapped[list[KitItem]] = relationship(back_populates="kit", cascade="all, delete-orphan",
                                                order_by="KitItem.position")


class KitItem(Base):
    __tablename__ = "kit_items"
    __table_args__ = (
        CheckConstraint(_in("kind", KIT_ITEM_KINDS), name="ck_kit_item_kind"),
        CheckConstraint(_in("status", KIT_ITEM_STATUSES), name="ck_kit_item_status"),
        CheckConstraint("amount > 0", name="ck_kit_item_amount_positive"),
        Index("ix_kit_items_kit", "kit_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("onboarding_kits.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    kit: Mapped[OnboardingKit] = relationship(back_populates="items")


class KitPurchase(TimestampMixin, Base):
    """One purchase attempt for one kit item. Append-only (mirrors BenefitPurchase)."""

    __tablename__ = "kit_purchases"
    __table_args__ = (
        CheckConstraint(_in("status", SPEND_PURCHASE_STATUSES), name="ck_kit_purchase_status"),
        CheckConstraint(_in("mode", VIC_MODES), name="ck_kit_purchase_mode"),
        CheckConstraint("amount > 0", name="ck_kit_purchase_amount_positive"),
        Index("ix_kit_purchases_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    kit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("onboarding_kits.id", ondelete="CASCADE"), nullable=False, index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kit_items.id", ondelete="CASCADE"), nullable=False, index=True)
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
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_email: Mapped[str | None] = mapped_column(String(254))


class TripRequest(TimestampMixin, Base):
    """One contractor trip: a set of components approved as ONE snapshot."""

    __tablename__ = "trip_requests"
    __table_args__ = (
        CheckConstraint(_in("status", TRIP_STATUSES), name="ck_trip_request_status"),
        Index("ix_trip_requests_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    contractor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contractors.id", ondelete="SET NULL"))
    contractor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="REQUESTED")
    approval_hash: Mapped[str | None] = mapped_column(String(64))
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_email: Mapped[str | None] = mapped_column(String(254))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deny_reason: Mapped[str | None] = mapped_column(Text)
    caps_overridden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_email: Mapped[str | None] = mapped_column(String(254))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)

    components: Mapped[list[TripComponent]] = relationship(back_populates="trip", cascade="all, delete-orphan",
                                                           order_by="TripComponent.position")


class TripComponent(Base):
    __tablename__ = "trip_components"
    __table_args__ = (
        CheckConstraint(_in("kind", TRIP_COMPONENT_KINDS), name="ck_trip_component_kind"),
        CheckConstraint(_in("status", TRIP_COMPONENT_STATUSES), name="ck_trip_component_status"),
        CheckConstraint("amount > 0", name="ck_trip_component_amount_positive"),
        Index("ix_trip_components_trip", "trip_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    trip: Mapped[TripRequest] = relationship(back_populates="components")


class TripPurchase(TimestampMixin, Base):
    """One purchase attempt for one trip component. Append-only."""

    __tablename__ = "trip_purchases"
    __table_args__ = (
        CheckConstraint(_in("status", SPEND_PURCHASE_STATUSES), name="ck_trip_purchase_status"),
        CheckConstraint(_in("mode", VIC_MODES), name="ck_trip_purchase_mode"),
        CheckConstraint("amount > 0", name="ck_trip_purchase_amount_positive"),
        Index("ix_trip_purchases_org_employer", "organization_id", "employer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    employer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employers.id", ondelete="CASCADE"), nullable=False, index=True)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trip_components.id", ondelete="CASCADE"), nullable=False, index=True)
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
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_email: Mapped[str | None] = mapped_column(String(254))


# ---------------------------------------------------------------------------
# Email outbox — platform notifications for VIC spend modules (benefits,
# onboarding kits, travel) and payroll runs. Appended at the END of this
# module (additive only).
#
# The outbox row IS the evidence: a message is rendered and recorded
# first, and only a separate drain step may send it. Until SMTP is
# configured, rows are recorded as NOT_CONFIGURED — nothing is ever
# fake-sent. ``related_*`` is a polymorphic pointer (kind + label + id)
# to the kit / trip / benefit / run the message is about, in the style
# journals and evidence bundles use to identify entities by label.
# ---------------------------------------------------------------------------

EMAIL_OUTBOX_STATUSES = ("QUEUED", "SENT", "FAILED", "NOT_CONFIGURED")
EMAIL_RELATED_KINDS = ("kit", "trip", "benefit", "run", "payroll_run",
                       "employer", "contractor", "other")


class EmailOutbox(TimestampMixin, Base):
    """One rendered email, recorded before (and whether or not) it is sent."""

    __tablename__ = "email_outbox"
    __table_args__ = (
        CheckConstraint(_in("status", EMAIL_OUTBOX_STATUSES), name="ck_email_outbox_status"),
        CheckConstraint(_in("related_kind", EMAIL_RELATED_KINDS), name="ck_email_outbox_related_kind"),
        Index("ix_email_outbox_org_created", "organization_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    to_address: Mapped[str] = mapped_column(String(254), nullable=False)
    template: Mapped[str] = mapped_column(String(80), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="QUEUED")
    related_label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    related_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    # Polymorphic reference, stored as text: the related entity's id in
    # string form (a UUID for kits/trips/benefits/runs), never a FK.
    related_id: Mapped[str | None] = mapped_column(String(80))
    error_detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))



# ---------------------------------------------------------------------------
# Spend Workflows — deterministic workflow engine (additive only).
# Domain events fire saved node graphs; human gates park runs durably.
# NO LLM at runtime and NO approve/purchase/pay actions exist in the
# graph vocabulary — a gate waits for a human decision recorded elsewhere.
# ---------------------------------------------------------------------------

WORKFLOW_RUN_STATUSES = ("RUNNING", "WAITING", "COMPLETED", "FAILED")
WORKFLOW_STEP_STATUSES = ("OK", "FAILED", "SKIPPED", "WAITING", "RESUMED")
WORKFLOW_APPROVAL_STATUSES = ("PENDING", "APPROVED", "REJECTED")
WORKFLOW_TASK_KINDS = ("EXCEPTION", "REMINDER")
WORKFLOW_TASK_STATUSES = ("OPEN", "DONE")
WORKFLOW_TRIGGER_TYPES = (
    "trigger.employer_added", "trigger.contractor_joined", "trigger.payroll_held",
    "trigger.payroll_settled", "trigger.benefit_requested", "trigger.trip_proposed",
    "trigger.kit_approved", "trigger.kit_completed", "trigger.schedule",
    "trigger.funds_received",
)


class Workflow(TimestampMixin, Base):
    """One saved node graph. ``trigger_type`` is denormalised from the graph."""

    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_org_trigger", "organization_id", "trigger_type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trigger_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    graph: Mapped[dict | None] = mapped_column(JsonType)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # ``created_by`` in the lane contract = created_by_id (house naming).
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                                                 nullable=False)

    runs: Mapped[list[WorkflowRun]] = relationship(back_populates="workflow", cascade="all, delete-orphan")


class WorkflowRun(TimestampMixin, Base):
    """One execution of a workflow for one entity. Dedupe is DB-enforced."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        # dedupe_key encodes workflow+event+entity AND, for schedules, the
        # period stamp — so uniqueness lives on the key itself.
        UniqueConstraint("dedupe_key", name="uq_workflow_run_dedupe"),
        CheckConstraint(_in("status", WORKFLOW_RUN_STATUSES), name="ck_workflow_run_status"),
        Index("ix_workflow_runs_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    trigger_event: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    context: Mapped[dict | None] = mapped_column(JsonType)
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workflow: Mapped[Workflow] = relationship(back_populates="runs")
    steps: Mapped[list[WorkflowRunStep]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                        order_by="WorkflowRunStep.created_at")


class WorkflowRunStep(TimestampMixin, Base):
    """Append-only record of one node execution inside one run."""

    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        CheckConstraint(_in("status", WORKFLOW_STEP_STATUSES), name="ck_workflow_step_status"),
        Index("ix_workflow_steps_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(120), nullable=False)
    node_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output: Mapped[dict | None] = mapped_column(JsonType)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # NULL while a step is parked (WAITING); set when the step concludes.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")


class WorkflowApproval(TimestampMixin, Base):
    """The durable record a wait gate parks on. Decided by a human elsewhere."""

    __tablename__ = "workflow_approvals"
    __table_args__ = (
        CheckConstraint(_in("status", WORKFLOW_APPROVAL_STATUSES), name="ck_workflow_approval_status"),
        Index("ix_workflow_approvals_entity", "organization_id", "entity_kind", "entity_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    entity_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowTask(TimestampMixin, Base):
    """An operator task produced by a workflow (exception or reminder)."""

    __tablename__ = "workflow_tasks"
    __table_args__ = (
        CheckConstraint(_in("kind", WORKFLOW_TASK_KINDS), name="ck_workflow_task_kind"),
        CheckConstraint(_in("status", WORKFLOW_TASK_STATUSES), name="ck_workflow_task_status"),
        Index("ix_workflow_tasks_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    employer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("employers.id", ondelete="SET NULL"))
    entity_kind: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"))
