"""PlatformRuntime — the bridge between the LangGraph agents and the database.

One runtime per process holds the shared Airwallex client (simulator in
demo mode), the in-memory ledger mirror, the settlement chain, the Visa
TAP signer and the compiled graph. Every graph step is followed by a
``_sync`` that persists exactly what happened: run status, agent events,
pay lines, ledger transactions, Airwallex calls and Metal receipts.

Concurrency: graph invocations are serialised with a lock (the simulator
and ledger mirror are process-local). Run PaySilo with a single worker.

Durability: the database is the source of truth. In demo mode the
simulator is rehydrated from the persisted ledger on startup, so balances
survive restarts. A run paused at an approval lives in the LangGraph
checkpointer chosen by :func:`app.agents.checkpoint.build_checkpointer`:
with a Postgres ``DATABASE_URL`` or a configured ``CHECKPOINT_PATH`` the
pause is durable and the run resumes after a restart; with the in-memory
fallback it is marked not resumable and a new run must be started.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents.checkpoint import build_checkpointer
from ..agents.graph import Services, build_graph, initial_state, resume_failed
from ..airwallex.client import AirwallexClient
from ..airwallex.rates import LiveFxRateProvider
from ..airwallex.recording import RecordingAirwallex
from ..airwallex.simulator import PLATFORM_ACCOUNT_ID, USD_VALUE, AirwallexSimulator, request_id_for
from ..config import Settings
from ..db import Database
from ..db.models import (
    AgentEvent,
    AirwallexRequest,
    Approval,
    Contractor,
    Employer,
    LedgerEntry,
    LedgerTransaction,
    Organization,
    PayLine,
    PayrollRun,
    SettlementReceipt,
    TapKey,
    User,
)
from ..ledger import Ledger
from ..money import money_str, round_money, to_decimal
from ..risk import BANK_UNVERIFIED, RISK_FIELDS, RISK_MESSAGES
from ..partners.metal import MetalL2SettlementChain, MockSettlementChain
from ..partners.vic import build_vic_adapter
from ..partners.tap import load_public_key_pem, public_key_raw
from ..partners.tap_signing import (
    TapSigner,
    registry_from_tap_key_rows,
    register_active_key,
    signer_for_settings,
    verify_stored_approval,
)

logger = logging.getLogger(__name__)

TERMINAL = {"SETTLED", "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED", "FAILED", "PARTIALLY_PAID",
            "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING"}
# Money moved and the run is closed (possibly with a follow-up still owed).
MONEY_MOVED = {"SETTLED", "PARTIALLY_PAID", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING"}
_UUID_IN_MEMO = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


class RuntimeConflict(Exception):
    """A request that is valid in shape but not in the run's current state."""


class HeldLineUnavailable(Exception):
    """A held line cannot be paid as recorded (contractor deleted, deactivated or re-denominated)."""


# A "pay held lines" continuation that ended in one of these moved no money and holds nothing,
# so it does not block another attempt. (FAILED is retried via /retry; anything else is open or paid.)
CONTINUATION_NON_BLOCKING = {"REFUSED", "HELD_SHORTFALL", "HELD_BY_HUMAN", "APPROVAL_REJECTED"}
CONTINUABLE = {"PARTIALLY_PAID", "HELD_SHORTFALL"}


def _namespace(account: str) -> str:
    return account.split(":", 1)[0]


def _currency(account: str) -> str:
    return account.rsplit(":", 1)[-1]


def _employer_id_from_key(key: str) -> uuid.UUID | None:
    if key.startswith("emp_"):
        try:
            return uuid.UUID(hex=key[4:])
        except ValueError:
            return None
    return None


def _keyword(header: str, name: str) -> str | None:
    m = re.search(rf'{name}="([^"]+)"', header)
    return m.group(1) if m else None


class PlatformRuntime:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.lock = threading.RLock()
        # Live mode refuses to start without TAP_PRIVATE_KEY (TapKeyError); demo
        # mode falls back to an ephemeral key that will not verify after a restart.
        self.signer, self.tap_key_persistent = signer_for_settings(settings)
        if self.tap_key_persistent:
            logger.info("Visa TAP: persistent signing key loaded from TAP_PRIVATE_KEY (keyid %s).",
                        self.signer.key.key_id)
        else:
            logger.warning(
                "Visa TAP: TAP_PRIVATE_KEY is not set - using an ephemeral dev key (keyid %s). "
                "Signatures from previous processes will not verify after a restart.",
                self.signer.key.key_id)
        self.recorder = RecordingAirwallex(
            AirwallexSimulator() if settings.demo_mode else AirwallexClient(settings))
        self.ledger = Ledger()
        if settings.demo_mode:
            # Demo pricing uses the static demo table, as before.
            usd_rates, rate_provider = dict(USD_VALUE), None
        else:
            # Live pricing uses ONLY rates fetched from FX quotes on the
            # employer's own account. The base table holds just the USD
            # identity rate; the provider refuses when a rate is missing.
            usd_rates = {"USD": Decimal("1")}
            rate_provider = LiveFxRateProvider(self.recorder)
        if settings.metal_rpc_url and settings.metal_private_key and settings.metal_registry_address:
            # Live Metal L2 leg (fails closed on any RPC error). The mock
            # chain stays the default whenever Metal is not fully configured.
            self.chain = MetalL2SettlementChain(
                rpc_url=settings.metal_rpc_url, private_key=settings.metal_private_key,
                registry_address=settings.metal_registry_address,
                explorer_url=settings.metal_explorer_url)
            logger.info("Metal: live L2 attestation configured (registry %s).",
                        settings.metal_registry_address)
        else:
            self.chain = MockSettlementChain()
        self.vic = build_vic_adapter(settings)
        if self.vic.mode == "VIC":
            logger.info("Visa VIC: live mode (real airi-cli, two-phase mandate flow, test_card=%s). "
                        "Readiness is checked per call; it fails closed, never simulates.",
                        self.vic.test_card)
        else:
            logger.info("Visa VIC: VIC_MODE=simulated — benefits run in SIMULATED mode (%s).",
                        self.vic.credential_ref)
        self.services = Services(airwallex=self.recorder, ledger=self.ledger, chain=self.chain,
                                 usd_rates=usd_rates, rate_provider=rate_provider, tap=self.signer)
        self.checkpointer = build_checkpointer(database_url=settings.database_url,
                                               checkpoint_path=settings.checkpoint_path)
        self.graph = build_graph(self.services, checkpointer=self.checkpointer)
        self._live_threads: set[str] = set()
        self._ledger_synced = 0
        self._calls_synced = 0

    # ------------------------------------------------------------------ boot
    def startup(self) -> None:
        with self.db.sessionmaker() as s:
            # Register the active public key (retiring any previous one), then
            # load every non-revoked, non-expired key (active + retired) into the
            # verification registry so signatures made under a retired keyid keep
            # verifying after a rotation.
            register_active_key(s, self.signer.key)
            s.commit()
            rows = s.scalars(select(TapKey)).all()
            registry = registry_from_tap_key_rows(rows)
            if registry.lookup(self.signer.key.key_id) is None:
                registry.register(self.signer.key)
            self.signer.registry = registry
            self._hydrate(s)
            self._seed_chain(s)

    def _seed_chain(self, s: Session) -> None:
        """Continue the settlement hash chain across restarts.

        An in-process chain starts at genesis; seeding it with the last
        persisted receipt keeps ``prev_hash`` / ``index`` continuous. Skipped
        when the chain has no ``seed(prev_hash, index)`` or nothing is persisted.
        """
        seed = getattr(self.chain, "seed", None)
        if not callable(seed):
            return
        chain_name = "mock" if isinstance(self.chain, MockSettlementChain) else "metal-l2"
        last = s.scalar(select(SettlementReceipt).where(SettlementReceipt.chain == chain_name)
                        .order_by(SettlementReceipt.chain_index.desc(), SettlementReceipt.created_at.desc())
                        .limit(1))
        if last is not None:
            seed(prev_hash=last.receipt_hash, index=last.chain_index + 1)
            logger.info("Settlement chain %s seeded from receipt #%d", chain_name, last.chain_index)

    # ------------------------------------------------------------- Visa TAP
    @staticmethod
    def _tap_key_is_published(row: TapKey, now: datetime) -> bool:
        if row.revoked_at is not None:
            return False
        if row.expires_at is not None:
            expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
            if expires <= now:
                return False
        return True

    def published_tap_keys(self, s: Session) -> list[TapKey]:
        """Keys served at ``/.well-known/tap-keys``: active + retired-but-verifiable."""
        now = datetime.now(UTC)
        rows = s.scalars(select(TapKey).order_by(TapKey.created_at)).all()
        return [r for r in rows if self._tap_key_is_published(r, now)]

    def tap_keys_payload(self, s: Session) -> dict:
        keys = []
        for row in self.published_tap_keys(s):
            try:
                raw_key = base64.b64encode(public_key_raw(load_public_key_pem(row.public_key_pem))).decode("ascii")
            except Exception:
                logger.warning("Visa TAP: tap_keys row %s has an unusable public key; not published.", row.key_id)
                continue
            keys.append({
                "keyid": row.key_id,
                "agent_id": row.agent_id,
                "algorithm": row.algorithm,
                "status": "retired" if row.retired_at is not None else "active",
                "public_key": raw_key,
                "public_key_pem": row.public_key_pem,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "retired_at": row.retired_at.isoformat() if row.retired_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            })
        return {"keys": keys}

    def verify_stored_approval(self, s: Session, organization_id: uuid.UUID, *,
                               approval_id: uuid.UUID | None = None,
                               run_id: uuid.UUID | None = None) -> dict | None:
        """Verify one stored approval for the caller's organization only.

        Returns ``None`` when no matching approval exists in that
        organization (tenant isolation: another org's approval is a 404,
        never a verification result). The registry is rebuilt from the
        ``tap_keys`` table, so this works in a fresh process after a
        restart as long as the signing key was persistent.
        """
        q = (select(Approval).join(PayrollRun, Approval.run_id == PayrollRun.id)
             .where(PayrollRun.organization_id == organization_id))
        if approval_id is not None:
            q = q.where(Approval.id == approval_id)
        elif run_id is not None:
            q = q.where(Approval.run_id == run_id)
        else:
            return None
        approval = s.scalar(q.order_by(Approval.created_at.desc()))
        if approval is None:
            return None
        rows = [r for r in s.scalars(select(TapKey).where(TapKey.revoked_at.is_(None)))
                if self._tap_key_is_published(r, datetime.now(UTC))]
        result = verify_stored_approval(approval, registry_from_tap_key_rows(rows))
        return {
            "approval_id": str(approval.id),
            "run_id": str(approval.run_id),
            "verified": result.valid,
            "keyid": result.key_id or approval.tap_key_id,
            "agent_id": result.agent_id,
            "code": result.code,
            "reason": result.detail,
            "decided_by_email": approval.decided_by_email,
            "decision": approval.decision,
        }

    def _hydrate(self, s: Session) -> None:
        """Rebuild the ledger mirror (and the demo simulator) from the database."""
        txns = s.scalars(select(LedgerTransaction).order_by(LedgerTransaction.id)).all()
        for t in txns:
            self.ledger.post([(e.account, e.amount) for e in t.entries], memo=t.memo)
        self._ledger_synced = len(self.ledger.transactions)
        if not self.settings.demo_mode:
            return
        sim: AirwallexSimulator = self.recorder.inner
        for emp in s.scalars(select(Employer).where(Employer.airwallex_account_id.is_not(None))):
            sim.restore_account(emp.airwallex_account_id, business_name=emp.name,
                                balances=self._cash(emp.ledger_key))
        sim.restore_account(PLATFORM_ACCOUNT_ID, business_name="PaySilo Platform",
                            balances=self._cash("platform", account="fees"))
        logger.info("Rehydrated %d ledger transactions into the demo simulator", len(txns))

    def _cash(self, namespace: str, account: str = "cash") -> dict[str, Decimal]:
        prefix = f"{namespace}:{account}:"
        out: dict[str, Decimal] = {}
        for t in self.ledger.transactions:
            for e in t.entries:
                if e.account.startswith(prefix):
                    out[_currency(e.account)] = out.get(_currency(e.account), Decimal("0")) + e.amount
        return {c: round_money(v) for c, v in out.items() if v != 0}

    # ------------------------------------------------------------- employers
    def ensure_account(self, s: Session, employer: Employer) -> Employer:
        """Create + activate the employer's own connected account (its silo)."""
        if employer.airwallex_account_id:
            if not employer.airwallex_global_account_id:
                self._ensure_global_account(s, employer)  # account predates global accounts
            return employer
        with self.lock:
            account = self.recorder.create_connected_account(
                request_id=request_id_for(str(employer.id), "connected-account"),
                business_name=employer.name, contact_name=employer.contact_name or "",
                email=employer.contact_email or "")
            employer.airwallex_account_id = account["id"]
            if not self.settings.demo_mode:
                self._onboard_live_account(employer, account.get("status"))
            employer.status = "ACTIVE"
            self._flush_calls(s, employer.organization_id, employer.id, None)
        self._ensure_global_account(s, employer)
        return employer

    def _ensure_global_account(self, s: Session, employer: Employer) -> str:
        """Give the employer its own funding (global) account; deposits land here.

        The request id is deterministic, so a retry returns the same account.
        """
        if employer.airwallex_global_account_id:
            return employer.airwallex_global_account_id
        with self.lock:
            ga = self.recorder.create_global_account(
                request_id=request_id_for(str(employer.id), f"global-account:{employer.funding_currency}"),
                account_id=employer.airwallex_account_id, currency=employer.funding_currency,
                country_code="US" if employer.funding_currency == "USD" else (employer.country or "US"),
                nick_name=f"{employer.name} {employer.funding_currency}"[:60])
            employer.airwallex_global_account_id = ga["id"]
            self._flush_calls(s, employer.organization_id, employer.id, None)
        return employer.airwallex_global_account_id

    def _onboard_live_account(self, employer: Employer, account_status: str | None = None) -> None:
        """Onboard a live connected account and force it to ACTIVE.

        Hosted onboarding comes first when a template is configured: the
        flow is created and authorised so the employer can complete the
        Airwallex-hosted form. In the sandbox the account is then moved to
        ACTIVE through the simulation status endpoint — strictly before
        any deposit, FX or transfer for this employer.

        ``create_connected_account`` on the live client already runs the whole
        Kit 6 activation (create -> update/EIN -> submit -> simulate ACTIVE ->
        confirm) and returns ACTIVE; the extra forced activation is skipped then.
        """
        account_id = employer.airwallex_account_id
        template = self.settings.airwallex_hosted_onboarding_template
        if template:
            flow = self.recorder.create_hosted_onboarding_link(
                account_id=account_id, template=template,
                return_url=self.settings.airwallex_onboarding_return_url or "")
            self.recorder.authorize_hosted_onboarding_link(flow_id=flow["id"])
            logger.info("Hosted onboarding flow %s created and authorised for employer %s",
                        flow["id"], employer.id)
        if account_status == "ACTIVE":
            return
        activated = self.recorder.activate_connected_account(account_id=account_id)
        if activated.get("status") != "ACTIVE":
            raise RuntimeError(
                f"Connected account {account_id} did not reach ACTIVE "
                f"(status: {activated.get('status')}); refusing to move money for "
                f"{employer.name}."
            )

    def fund(self, s: Session, employer: Employer, amount: Decimal, reference: str) -> None:
        """Simulated deposit into the employer's OWN wallet (sandbox simulation API)."""
        amount = round_money(amount)
        if amount <= 0:
            raise ValueError("Funding amount must be positive.")
        self.ensure_account(s, employer)
        request_id = request_id_for(str(employer.id), f"deposit:{reference}")
        if s.scalar(select(AirwallexRequest.id).where(AirwallexRequest.request_id == request_id)):
            # Same reference = same request_id: Airwallex would return the original
            # deposit, so the ledger must not be credited a second time either.
            raise RuntimeConflict(f"Deposit '{reference}' was already processed for {employer.name}.")
        with self.lock:
            self.recorder.deposit(request_id=request_id,
                                  account_id=employer.airwallex_account_id, amount=amount,
                                  currency=employer.funding_currency,
                                  global_account_id=employer.airwallex_global_account_id or "")
            self.ledger.post([(f"{employer.ledger_key}:cash:{employer.funding_currency}", amount),
                              (f"external:bank:{employer.funding_currency}", -amount)],
                             memo=f"deposit {reference}")
            self._flush_calls(s, employer.organization_id, employer.id, None)
            self._flush_ledger(s, {employer.ledger_key: (employer.organization_id, employer.id)})

    def balances(self, s: Session, employer: Employer) -> dict[str, str]:
        """Derived from persisted ledger entries — never from a stored balance."""
        rows = s.execute(
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .where(LedgerEntry.account.like(f"{employer.ledger_key}:cash:%"))
            .group_by(LedgerEntry.currency)
        ).all()
        return {ccy: money_str(total) for ccy, total in rows if total and Decimal(total) != 0}

    def post_employer_journal(self, s: Session, employer: Employer,
                              entries: list[tuple[str, object]], memo: str) -> LedgerTransaction:
        """Post one balanced journal for ONE employer and persist it directly.

        Unlike :meth:`_flush_ledger` (which infers ``run_id`` from memo
        UUIDs for graph-driven postings), this writes the DB row itself,
        with ``run_id=None`` — for non-run postings such as benefit
        purchases, whose accounts (``benefits_*``) never touch ``cash:``.
        """
        with self.lock:
            txn = self.ledger.post(entries, memo=memo)
            row = LedgerTransaction(
                organization_id=employer.organization_id, employer_id=employer.id, run_id=None,
                memo=memo[:200],
                entries=[LedgerEntry(account=e.account, namespace=_namespace(e.account),
                                     currency=_currency(e.account), amount=e.amount) for e in txn.entries])
            s.add(row)
            s.flush()
            self._ledger_synced = len(self.ledger.transactions)
            return row

    def platform_fees(self, s: Session, organization_id: uuid.UUID) -> dict[str, str]:
        rows = s.execute(
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .join(LedgerTransaction)
            .where(LedgerTransaction.organization_id == organization_id,
                   LedgerEntry.account.like("platform:fees:%"))
            .group_by(LedgerEntry.currency)
        ).all()
        return {ccy: money_str(total) for ccy, total in rows if total}

    # ------------------------------------------------------------------ runs
    def start_run(self, s: Session, *, org: Organization, user: User, employer: Employer,
                  pay_lines: list[dict] | None, raw_input: str | None, source: str,
                  period_label: str | None = None) -> PayrollRun:
        self.ensure_account(s, employer)
        with self.lock:  # check-then-create must be atomic: one open run per employer
            self._reject_if_open_run(s, employer)
            return self._start_run_locked(s, org=org, user=user, employer=employer, pay_lines=pay_lines,
                                          raw_input=raw_input, source=source, period_label=period_label)

    # ------------------------------------------------------- held lines
    @staticmethod
    def held_pay_lines(run: PayrollRun) -> list[PayLine]:
        return [pl for pl in sorted(run.pay_lines, key=lambda p: p.position) if pl.status == "HELD"]

    def continuations_of(self, s: Session, run: PayrollRun) -> list[PayrollRun]:
        """Runs that continue ``run`` (same organization), newest first."""
        return list(s.scalars(select(PayrollRun)
                              .where(PayrollRun.parent_run_id == run.id,
                                     PayrollRun.organization_id == run.organization_id)
                              .order_by(PayrollRun.created_at.desc())))

    def run_fee_collected(self, s: Session, run: PayrollRun) -> bool:
        """True when the run fee was already collected on ``run`` or any run up its parent chain."""
        seen: set[uuid.UUID] = set()
        current: PayrollRun | None = run
        while current is not None and current.id not in seen:
            if current.fee_charged_usd and current.fee_charged_usd > 0:
                return True
            seen.add(current.id)
            current = (s.scalar(select(PayrollRun).where(PayrollRun.id == current.parent_run_id,
                                                         PayrollRun.organization_id == run.organization_id))
                       if current.parent_run_id else None)
        return False

    def pay_held_blocker(self, s: Session, run: PayrollRun) -> str | None:
        """Why ``run``'s held lines cannot be paid right now (None when they can). Role-independent."""
        held = self.held_pay_lines(run)
        if run.status not in CONTINUABLE:
            return (f"Only a partially paid or held-for-shortfall run has held lines to pay; this run is "
                    f"{run.status}.")
        if not held:
            return "This run has no held lines to pay."
        if run.employer.status != "ACTIVE":
            return f"{run.employer.name} is not active."
        for cont in self.continuations_of(s, run):
            if cont.status in CONTINUATION_NON_BLOCKING:
                continue
            if cont.status == "RESERVED" and cont.pending_interrupt is not None and not self.is_resumable(cont):
                continue  # paused before a restart and lost: holds nothing
            if cont.status == "FAILED":
                return (f"The continuation (run {str(cont.id)[:8]}) failed part-way; retry that run "
                        "instead of starting another.")
            return (f"Held lines of this run are already being paid or were paid by run "
                    f"{str(cont.id)[:8]} ({cont.status}).")
        ids = [pl.contractor_id for pl in held if pl.contractor_id]
        if ids:
            statuses = dict(s.execute(select(Contractor.id, Contractor.bank_verification_status).where(
                Contractor.id.in_(ids), Contractor.organization_id == run.organization_id)).all())
            if all(statuses.get(pl.contractor_id, "VERIFIED") != "VERIFIED" for pl in held):
                return (f"{RISK_MESSAGES[BANK_UNVERIFIED]} Verify the bank details first, then pay the held lines.")
        if ids:
            later = s.execute(
                select(PayLine.beneficiary_name)
                .join(PayrollRun, PayLine.run_id == PayrollRun.id)
                .where(PayrollRun.employer_id == run.employer_id,
                       PayrollRun.organization_id == run.organization_id,
                       PayrollRun.created_at > run.created_at, PayrollRun.id != run.id,
                       PayLine.status == "PAID", PayLine.contractor_id.in_(ids))).scalars().all()
            if later:
                names = ", ".join(sorted(set(later)))
                return (f"A later payroll already paid {names}; paying the held line would pay them "
                        "twice. Start a normal run instead.")
        return None

    def _continuation_lines(self, s: Session, run: PayrollRun) -> list[dict]:
        """Rebuild the held lines from the CURRENT contractor records (corrected bank details count)."""
        lines, problems = [], []
        for pl in self.held_pay_lines(run):
            c = (s.scalar(select(Contractor).where(Contractor.id == pl.contractor_id,
                                                   Contractor.employer_id == run.employer_id))
                 if pl.contractor_id else None)
            if c is None:
                problems.append(f"{pl.beneficiary_name} is no longer a contractor of {run.employer.name}")
            elif not c.is_active:
                problems.append(f"{c.full_name} is deactivated")
            elif c.currency != pl.currency:
                problems.append(f"{c.full_name} is now paid in {c.currency}, not {pl.currency}")
            else:
                lines.append(self.contractor_line(c, pl.amount))
        if problems:
            raise HeldLineUnavailable(
                "Cannot pay the held lines: " + "; ".join(problems) + ". Fix the contractor or start a new run.")
        return lines

    def start_continuation(self, s: Session, *, org: Organization, user: User, run: PayrollRun) -> PayrollRun:
        """Start a NEW run that pays only ``run``'s HELD lines (the original is never touched).

        Goes through the same pricing -> TAP-approval -> payout path as any run. The run fee is waived
        when it was already collected up the parent chain.
        """
        employer = run.employer
        self.ensure_account(s, employer)
        with self.lock:
            reason = self.pay_held_blocker(s, run)
            if reason:
                raise RuntimeConflict(reason)
            lines = self._continuation_lines(s, run)
            self._reject_if_open_run(s, employer)
            return self._start_run_locked(
                s, org=org, user=user, employer=employer, pay_lines=lines, raw_input=None,
                source="continuation", period_label=run.period_label, parent_run_id=run.id,
                waive_run_fee=self.run_fee_collected(s, run))

    def _start_run_locked(self, s: Session, *, org: Organization, user: User, employer: Employer,
                          pay_lines: list[dict] | None, raw_input: str | None, source: str,
                          period_label: str | None, parent_run_id: uuid.UUID | None = None,
                          waive_run_fee: bool = False) -> PayrollRun:
        run = PayrollRun(organization_id=org.id, employer_id=employer.id, created_by_id=user.id,
                         source=source, raw_input=raw_input, period_label=period_label, status="RECEIVED",
                         parent_run_id=parent_run_id)
        s.add(run)
        s.flush()
        active = [c for c in employer.contractors if c.is_active]
        facts = self.risk_facts(s, run.organization_id, active)
        directory = [
            {"name": c.full_name, "currency": c.currency, "method": c.payout_method, "iban": c.iban,
             "swift_code": c.swift_code, "account_number": c.account_number, "routing_code": c.routing_code,
             "country_code": c.country, "contractor_id": str(c.id), **facts[c.id]}
            for c in active
        ]
        pay_lines = self._attach_risk_facts(pay_lines, {str(k): v for k, v in facts.items()})
        state = initial_state(
            run_id=str(run.id), employer_key=employer.ledger_key, employer_name=employer.name,
            contact_name=employer.contact_name or "", funding_usd="0", pay_lines=pay_lines or [],
            raw_input=raw_input, account_id=employer.airwallex_account_id, directory=directory,
            parent_run_id=str(parent_run_id) if parent_run_id else None, waive_run_fee=waive_run_fee)
        with self.lock:
            config = {"configurable": {"thread_id": str(run.id)}}
            self._live_threads.add(str(run.id))
            self._invoke_and_persist(s, run, employer, lambda: self.graph.invoke(state, config))
        return run

    @staticmethod
    def _attach_risk_facts(pay_lines: list[dict] | None, facts: dict[str, dict]) -> list[dict] | None:
        """Stamp each given line with the CURRENT risk facts of its contractor (server-side, never client-supplied).

        A line naming a contractor that is not one of this employer's active contractors is stamped
        ``bank_status="UNKNOWN"`` so the risk gate holds it (fail closed).
        """
        if not pay_lines:
            return pay_lines
        out = []
        for line in pay_lines:
            line = {k: v for k, v in line.items() if k not in (*RISK_FIELDS, "hold_reason")}
            cid = line.get("contractor_id")
            if cid:
                line.update(facts.get(str(cid), {"bank_status": "UNKNOWN"}))
            out.append(line)
        return out

    def risk_facts(self, s: Session, org_id: uuid.UUID, contractors: list[Contractor]) -> dict[uuid.UUID, dict]:
        """Per contractor: the strings the risk gate needs (bank status, change time, second verification, last paid)."""
        out: dict[uuid.UUID, dict] = {}
        for c in contractors:
            f: dict[str, str] = {"bank_status": c.bank_verification_status}
            changed = c.bank_details_changed_at
            if changed is not None:
                changed = changed if changed.tzinfo else changed.replace(tzinfo=UTC)
                f["bank_changed_at"] = changed.isoformat()
                verified = c.bank_verified_at
                verified = verified if verified is None or verified.tzinfo else verified.replace(tzinfo=UTC)
                second = (c.bank_verification_status == "VERIFIED" and c.bank_verified_by_id is not None
                          and c.bank_verified_by_id != c.bank_change_requested_by_id
                          and verified is not None and verified >= changed)
                f["bank_second_verified"] = "yes" if second else "no"
            prev = s.scalar(
                select(PayLine.amount).join(PayrollRun, PayLine.run_id == PayrollRun.id)
                .where(PayLine.contractor_id == c.id, PayLine.status == "PAID", PayLine.currency == c.currency,
                       PayrollRun.organization_id == org_id)
                .order_by(PayrollRun.created_at.desc(), PayLine.position.desc()).limit(1))
            if prev is not None:
                f["prev_paid"] = money_str(prev)
            out[c.id] = f
        return out

    def _announce_outcome(self, s: Session, run: PayrollRun) -> None:
        """Queue the run_settled / payment_failed notice right away (same dedupe keys as the sweep). Never raises."""
        if run.status not in ("SETTLED", "FAILED"):
            return
        try:
            from . import autopilot, notify  # lazy: autopilot imports this module

            org = s.get(Organization, run.organization_id)
            if org is not None and autopilot.report_run_outcome(s, self.settings, org, run):
                s.commit()
                notify.dispatch_async(self.db.sessionmaker, self.settings)
        except Exception:  # noqa: BLE001 - a mail problem must never affect the run
            logger.exception("Queueing the run outcome notice failed for run %s", run.id)
            s.rollback()

    def _invoke_and_persist(self, s: Session, run: PayrollRun, employer: Employer, call,
                            approval: Approval | None = None) -> None:
        """Invoke the graph and ALWAYS persist what happened, even if it raises.

        Money may already have moved when a node raises; the checkpoint,
        the ledger mirror and the recorded Airwallex calls are the truth, so
        they are synced and committed before the error propagates.
        """
        result: dict = {}
        failure: BaseException | None = None
        try:
            result = call() or {}
        except Exception as exc:  # noqa: BLE001 - persisted, then re-raised below
            failure = exc
            logger.exception("Run %s: graph invocation raised; persisting state before re-raising", run.id)
        try:
            self._sync(s, run, employer, result)
            if failure is not None and not run.error:
                run.error = f"{type(failure).__name__}: {failure}"[:500]
            if approval is not None:
                approval.verified = approval.kind == "approval" and run.status != "APPROVAL_REJECTED"
                s.add(approval)
        finally:
            s.commit()
        self._announce_outcome(s, run)
        if failure is not None:
            raise failure

    def _reject_if_open_run(self, s: Session, employer: Employer) -> None:
        """One open run per employer: a run awaiting a decision holds its reserve."""
        # Graph invocations are serialised by ``self.lock`` and every one is persisted when it
        # returns, so the only state a competing run can observe is RESERVED (paused at the
        # approval). A RESERVED run whose pause did not survive a restart holds nothing.
        q = select(PayrollRun).where(PayrollRun.employer_id == employer.id, PayrollRun.status == "RESERVED")
        for other in s.scalars(q):
            if not self.is_resumable(other):
                continue
            raise RuntimeConflict(
                f"{employer.name} already has an open run awaiting a decision. Approve or hold it "
                "before starting another payroll for this employer.")

    def decide(self, s: Session, *, run: PayrollRun, employer: Employer, user: User, decision: str) -> PayrollRun:
        if run.pending_interrupt is None:
            raise RuntimeConflict("This run is not waiting for a decision.")
        if not self.is_resumable(run):
            raise RuntimeConflict("This run was paused before the server restarted and can no longer be "
                                  "resumed. Start a new run for this employer.")
        kind = run.pending_interrupt.get("kind")
        if kind == "escalation" and decision != "hold":
            raise RuntimeConflict("A short payroll can only be held. Fund the employer's own wallet, "
                                  "then start a new run.")
        config = {"configurable": {"thread_id": str(run.id)}}
        with self.lock:
            state = self.graph.get_state(config).values
            if kind == "approval" and decision == "approve":
                self._reject_changed_bank_details(s, run, state)
            resume: dict = {"decision": decision, "decided_by": user.email}
            approval = Approval(run_id=run.id, kind=kind, decision=decision, decided_by_id=user.id,
                                decided_by_email=user.email)
            if kind == "approval":
                envelope = self.signer.sign_approval(
                    run_id=str(run.id), employer_key=employer.ledger_key, decision=decision,
                    assessment=state["assessment"], pay_lines=state["pay_lines"], decided_by=user.email,
                    held_lines=state.get("held_lines") or [])
                resume["tap"] = envelope.as_dict()
                sig_input = envelope.headers["Signature-Input"]
                approval.signed_body = envelope.body
                approval.tap_signature_input = sig_input
                approval.tap_signature = envelope.headers["Signature"]
                approval.tap_key_id = _keyword(sig_input, "keyid")
                approval.tap_nonce = _keyword(sig_input, "nonce")
            self._invoke_and_persist(s, run, employer, lambda: self.graph.invoke(Command(resume=resume), config),
                                     approval=approval)
        return run

    def _reject_changed_bank_details(self, s: Session, run: PayrollRun, state: dict) -> None:
        """Bank details must not change between the preview and the approval (re-read from the database)."""
        lines = [l for l in state.get("pay_lines") or [] if l.get("contractor_id")]
        if not lines:
            return
        ids = [uuid.UUID(l["contractor_id"]) for l in lines]
        contractors = s.scalars(select(Contractor).where(Contractor.id.in_(ids),
                                                         Contractor.organization_id == run.organization_id,
                                                         Contractor.employer_id == run.employer_id)).all()
        fresh = self.risk_facts(s, run.organization_id, list(contractors))
        for line in lines:
            now = fresh.get(uuid.UUID(line["contractor_id"]))
            if now is None or now["bank_status"] != "VERIFIED" or now.get("bank_changed_at") != line.get("bank_changed_at"):
                raise RuntimeConflict(
                    f"{line['name']}'s bank details changed after this payroll was prepared. {RISK_MESSAGES[BANK_UNVERIFIED]} "
                    "Hold this run and start a new one once they are verified.")

    def is_resumable(self, run: PayrollRun) -> bool:
        if run.pending_interrupt is None:
            return False
        if str(run.id) in self._live_threads:
            return True
        # Durable checkpointer: the pause survives a restart if the
        # checkpointer still holds this thread with a pending next step.
        try:
            snapshot = self.graph.get_state({"configurable": {"thread_id": str(run.id)}})
        except Exception:
            return False
        return bool(snapshot and snapshot.values and snapshot.next)

    def resume_failed(self, s: Session, *, run: PayrollRun, employer: Employer) -> PayrollRun:
        """Resume a FAILED run from its checkpoint without duplicating payments."""
        if run.status != "FAILED":
            raise RuntimeConflict("Only a FAILED run can be resumed.")
        config = {"configurable": {"thread_id": str(run.id)}}
        with self.lock:
            self._invoke_and_persist(s, run, employer, lambda: resume_failed(self.graph, config))
        return run

    # ------------------------------------------------------------------ sync
    def _sync(self, s: Session, run: PayrollRun, employer: Employer, result: dict) -> None:
        config = {"configurable": {"thread_id": str(run.id)}}
        values = dict(self.graph.get_state(config).values)
        a = values.get("assessment") or None

        run.status = values.get("status", run.status)
        run.assessment = a
        run.explanation = values.get("explanation")
        run.error = values.get("error")
        run.fee_charged_usd = to_decimal(values.get("fee_charged_usd") or "0")
        if a:
            run.payroll_currency = a.get("payroll_currency")
            run.required_usd = to_decimal(a["required_usd"])
            run.shortfall_usd = to_decimal(a.get("full_shortfall_usd") or a["shortfall_usd"])
        run.pending_interrupt = result["__interrupt__"][0].value if "__interrupt__" in result else None
        if run.status in MONEY_MOVED and run.settled_at is None:
            run.settled_at = datetime.now(UTC)
        if run.status in TERMINAL:
            self._live_threads.discard(str(run.id))

        # Agent events (append-only, ordered).
        known = s.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.run_id == run.id)) or 0
        for seq, e in enumerate(values.get("events", [])[known:], start=known):
            s.add(AgentEvent(run_id=run.id, organization_id=run.organization_id, seq=seq,
                             agent=e["agent"], level=e["level"], message=e["message"]))

        # Pay lines (created once the intake agent has produced them).
        existing = {pl.position: pl for pl in run.pay_lines}
        for i, line in enumerate(values.get("pay_lines", [])):
            pl = existing.get(i)
            if pl is None:
                contractor_id = line.get("contractor_id")
                pl = PayLine(run_id=run.id, position=i, beneficiary_name=line["name"],
                             amount=to_decimal(line["amount"]), currency=line["currency"],
                             method=line.get("method", "SWIFT"),
                             contractor_id=uuid.UUID(contractor_id) if contractor_id else None,
                             request_id=request_id_for(str(run.id), f"pay-{i}"))
                s.add(pl)
                run.pay_lines.append(pl)
            if run.status in ("HELD_SHORTFALL", "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED"):
                pl.status = "HELD"

        # Partial payout: the lines this run does not pay are persisted too, HELD.
        base = len(values.get("pay_lines", []))
        for j, line in enumerate(values.get("held_lines") or []):
            if (base + j) in existing:
                continue
            contractor_id = line.get("contractor_id")
            held = PayLine(run_id=run.id, position=base + j, beneficiary_name=line["name"],
                           amount=to_decimal(line["amount"]), currency=line["currency"],
                           method=line.get("method", "SWIFT"),
                           contractor_id=uuid.UUID(contractor_id) if contractor_id else None,
                           request_id=request_id_for(str(run.id), f"held-{base + j}"), status="HELD",
                           hold_reason=line.get("hold_reason"))
            s.add(held)
            run.pay_lines.append(held)

        calls = self.recorder.log[self._calls_synced:]
        transfers = {c.request_id: c.response for c in calls if c.operation == "create_transfer"}
        for pl in run.pay_lines:
            if pl.request_id in transfers:
                pl.airwallex_transfer_id = transfers[pl.request_id].get("id")
                pl.payout_fee = to_decimal(str(transfers[pl.request_id].get("fee_amount", "0")))
                pl.status = "PAID"
            elif run.status == "FAILED" and pl.status == "PENDING":
                pl.status = "FAILED"

        self._flush_calls(s, run.organization_id, employer.id, run.id)
        self._flush_ledger(s, {employer.ledger_key: (run.organization_id, employer.id)})

        receipt = values.get("metal_receipt")
        if receipt and not s.scalar(select(SettlementReceipt.id).where(SettlementReceipt.run_id == run.id)):
            s.add(SettlementReceipt(organization_id=run.organization_id, employer_id=employer.id, run_id=run.id,
                                    chain=receipt["chain"], chain_index=receipt["index"],
                                    payload_hash=receipt["payload_hash"], prev_hash=receipt["prev_hash"],
                                    receipt_hash=receipt["receipt_hash"], tx_hash=receipt.get("tx_hash")))
        s.flush()

    def _flush_calls(self, s: Session, org_id: uuid.UUID, employer_id: uuid.UUID | None,
                     run_id: uuid.UUID | None) -> None:
        calls = self.recorder.log[self._calls_synced:]
        self._calls_synced = len(self.recorder.log)
        if not calls:
            return
        seen = set(s.scalars(select(AirwallexRequest.request_id)
                             .where(AirwallexRequest.request_id.in_([c.request_id for c in calls]))))
        for c in calls:
            if c.request_id in seen:
                continue  # an idempotent replay: Airwallex returned the original result
            seen.add(c.request_id)
            s.add(AirwallexRequest(request_id=c.request_id, organization_id=org_id, employer_id=employer_id,
                                   run_id=run_id, operation=c.operation, on_behalf_of=c.on_behalf_of,
                                   response=c.response))

    def _flush_ledger(self, s: Session, owners: dict[str, tuple[uuid.UUID, uuid.UUID]]) -> None:
        new = self.ledger.transactions[self._ledger_synced:]
        self._ledger_synced = len(self.ledger.transactions)
        for t in new:
            keys = {_namespace(e.account) for e in t.entries} - {"platform", "external"}
            key = next(iter(keys), None)
            if key is None or key not in owners:
                emp_id = _employer_id_from_key(key) if key else None
                emp = s.get(Employer, emp_id) if emp_id else None
                if emp is None:
                    raise RuntimeError(f"Ledger transaction '{t.memo}' has no owning employer")
                owner = (emp.organization_id, emp.id)
            else:
                owner = owners[key]
            m = _UUID_IN_MEMO.match(t.memo)
            run_id = uuid.UUID(m.group(1)) if m else None
            s.add(LedgerTransaction(
                organization_id=owner[0], employer_id=owner[1], run_id=run_id, memo=t.memo[:200],
                entries=[LedgerEntry(account=e.account, namespace=_namespace(e.account),
                                     currency=_currency(e.account), amount=e.amount) for e in t.entries]))

    # ------------------------------------------------------------------ misc
    @staticmethod
    def contractor_line(c: Contractor, amount: Decimal) -> dict:
        return {"name": c.full_name, "amount": money_str(amount), "currency": c.currency,
                "method": c.payout_method, "iban": c.iban, "swift_code": c.swift_code,
                "account_number": c.account_number, "routing_code": c.routing_code,
                "country_code": c.country, "contractor_id": str(c.id)}
