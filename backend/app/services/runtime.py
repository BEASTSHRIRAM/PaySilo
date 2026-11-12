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
import hashlib
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
from ..partners.metal import MetalL2SettlementChain, MockSettlementChain
from ..partners.vic import build_vic_adapter
from ..partners.tap import load_public_key_pem, public_key_raw
from ..partners.tap_signing import (
    TapSigner,
    canonical_json,
    registry_from_tap_key_rows,
    register_active_key,
    signer_for_settings,
    verify_stored_approval,
)
from ..policy import PAYROLL_POLICY, ContractorPayroll, line_cost_usd, walk_priority

logger = logging.getLogger(__name__)

TERMINAL = {"SETTLED", "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED", "FAILED", "PARTIALLY_PAID",
            "PARTIALLY_SETTLED", "SENT_FEE_FAILED", "SENT_UNRECONCILED", "ATTEST_PENDING"}
# Money moved and the run is closed (possibly with a follow-up still owed).
MONEY_MOVED = {"SETTLED", "PARTIALLY_PAID", "PARTIALLY_SETTLED", "SENT_FEE_FAILED", "SENT_UNRECONCILED",
               "ATTEST_PENDING"}
_UUID_IN_MEMO = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


class RuntimeConflict(Exception):
    """A request that is valid in shape but not in the run's current state."""


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
        if settings.vic_credential:
            if self.vic.cli_available:
                logger.info("Visa VIC: live Airi adapter configured (credential %s, test_card=%s).",
                            self.vic.credential_ref, self.vic.test_card)
            else:
                logger.warning(
                    "Visa VIC: VIC_CREDENTIAL is set but the Airi CLI is not available "
                    "(set AIRI_CLI_PATH) — benefit purchases will fail closed, never simulate.")
        else:
            logger.info("Visa VIC: no VIC_CREDENTIAL set — benefits run in SIMULATED mode (%s).",
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
        return employer

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
                                  currency=employer.funding_currency)
            self.ledger.post([(f"{employer.ledger_key}:cash:{employer.funding_currency}", amount),
                              (f"external:bank:{employer.funding_currency}", -amount)],
                             memo=f"deposit {reference}")
            self._flush_calls(s, employer.organization_id, employer.id, None)
            self._flush_ledger(s, {employer.ledger_key: (employer.organization_id, employer.id)})
            # Spend-workflows dispatch (WF lane): trigger.funds_received.
            # Guarded + savepoint-isolated: a workflow error must NEVER
            # break a deposit (log only; the nested block rolls back alone).
            try:
                with s.begin_nested():
                    from .workflows import dispatch_event

                    balances_after = self.balances(s, employer)
                    dispatch_event(
                        s, organization_id=employer.organization_id,
                        event_type="funds.received", entity_kind="employer", entity_id=str(employer.id),
                        facts={"employer_id": str(employer.id), "employer_name": employer.name,
                               "amount": str(amount), "currency": employer.funding_currency,
                               "balance_after": balances_after.get(employer.funding_currency),
                               "contact_email": employer.contact_email, "status": employer.status})
            except Exception:  # noqa: BLE001
                logger.exception("workflow funds.received dispatch failed for employer %s (ignored)",
                                 employer.id)

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

    def _start_run_locked(self, s: Session, *, org: Organization, user: User, employer: Employer,
                          pay_lines: list[dict] | None, raw_input: str | None, source: str,
                          period_label: str | None) -> PayrollRun:
        run = PayrollRun(organization_id=org.id, employer_id=employer.id, created_by_id=user.id,
                         source=source, raw_input=raw_input, period_label=period_label, status="RECEIVED")
        s.add(run)
        s.flush()
        directory = [
            {"name": c.full_name, "currency": c.currency, "method": c.payout_method, "iban": c.iban,
             "swift_code": c.swift_code, "account_number": c.account_number, "routing_code": c.routing_code,
             "country_code": c.country, "contractor_id": str(c.id), "priority": c.priority}
            for c in employer.contractors if c.is_active
        ]
        state = initial_state(
            run_id=str(run.id), employer_key=employer.ledger_key, employer_name=employer.name,
            contact_name=employer.contact_name or "", funding_usd="0", pay_lines=pay_lines or [],
            raw_input=raw_input, account_id=employer.airwallex_account_id, directory=directory,
            settlement_mode=employer.settlement_mode)
        with self.lock:
            config = {"configurable": {"thread_id": str(run.id)}}
            self._live_threads.add(str(run.id))
            self._invoke_and_persist(s, run, employer, lambda: self.graph.invoke(state, config))
        return run

    def _invoke_and_persist(self, s: Session, run: PayrollRun, employer: Employer, call,
                            approval: Approval | None = None) -> None:
        """Invoke the graph and ALWAYS persist what happened, even if it raises.

        Money may already have moved when a node raises; the checkpoint,
        the ledger mirror and the recorded Airwallex calls are the truth, so
        they are synced and committed before the error propagates.
        """
        result: dict = {}
        failure: BaseException | None = None
        _prev_status = run.status  # spend-workflows: detect HELD/SETTLED transitions
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
        # Spend-workflows dispatch (WF lane): fire payroll_held /
        # payroll_settled on a persisted status transition. Guarded inside
        # dispatch_payroll_status (log, never raise) so a workflow error
        # can NEVER break payroll.
        if run.status != _prev_status and run.status in ("HELD_SHORTFALL", "HELD_BY_HUMAN", "SETTLED"):
            try:
                from .workflows import dispatch_payroll_status

                dispatch_payroll_status(s, run)
                s.commit()
            except Exception:  # noqa: BLE001 - belt-and-braces; helper already guards
                logger.exception("workflow payroll dispatch failed for run %s (ignored)", run.id)
                s.rollback()
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
            if a.get("residual_shortfall_usd") is not None:
                run.residual_shortfall_usd = to_decimal(a["residual_shortfall_usd"])
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
                           request_id=request_id_for(str(run.id), f"held-{base + j}"), status="HELD")
            s.add(held)
            run.pay_lines.append(held)

        calls = self.recorder.log[self._calls_synced:]
        transfers = {c.request_id: c.response for c in calls if c.operation == "create_transfer"}
        for pl in run.pay_lines:
            if pl.request_id in transfers:
                pl.airwallex_transfer_id = transfers[pl.request_id].get("id")
                pl.payout_fee = to_decimal(str(transfers[pl.request_id].get("fee_amount", "0")))
                pl.status = "PAID"
                if pl.settled_phase is None:
                    pl.settled_phase = 1  # settled by the original run execution
                    pl.settled_at = datetime.now(UTC)
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
                "country_code": c.country, "contractor_id": str(c.id), "priority": c.priority}

    # ------------------------------------------------------- residual (phase 2)
    def _held_lines_in_priority_order(self, s: Session, run: PayrollRun) -> list[PayLine]:
        """The run's HELD lines, in contractor-priority order (position breaks ties)."""
        held = [pl for pl in run.pay_lines if pl.status == "HELD"]
        priorities: dict[uuid.UUID, int] = {}
        if held:
            ids = [pl.contractor_id for pl in held if pl.contractor_id]
            if ids:
                priorities = {c.id: c.priority for c in
                              s.scalars(select(Contractor).where(Contractor.id.in_(ids)))}
        return sorted(held, key=lambda pl: (priorities.get(pl.contractor_id, 0) if pl.contractor_id else 0,
                                            pl.position))

    def _available_usd(self, s: Session, employer: Employer) -> Decimal:
        rates = self.services.usd_rates
        total = Decimal("0")
        for ccy, amount in self.balances(s, employer).items():
            rate = rates.get(ccy)
            if rate is None:
                raise RuntimeConflict(f"No USD rate available for {ccy}; cannot price a residual plan.")
            total += to_decimal(amount) * rate
        return round_money(total)

    def _compute_residual_plan(self, s: Session, run: PayrollRun, employer: Employer) -> dict:
        """Recompute the phase-2 walk over the run's HELD lines ONLY.

        No second platform fee: the run fee was charged once, in phase 1.
        Spendable = the employer's OWN current balance (USD terms) minus the
        reserve floor. Skip-and-continue in priority order, same as phase 1.
        """
        held = self._held_lines_in_priority_order(s, run)
        if not held:
            raise RuntimeConflict("This run has no held lines to rescue.")
        rates = self.services.usd_rates
        available = self._available_usd(s, employer)
        spendable = round_money(available - PAYROLL_POLICY.reserve_floor_usd)
        costs = [line_cost_usd(ContractorPayroll(name=pl.beneficiary_name, amount=pl.amount,
                                                 currency=pl.currency, method=pl.method),
                               rates, PAYROLL_POLICY) for pl in held]
        taken, remaining = walk_priority(costs, spendable, Decimal("0"))
        if not taken:
            raise RuntimeConflict(
                "No held line fits yet: fund this employer's own wallet first "
                f"(residual shortfall USD {money_str(run.residual_shortfall_usd or 0)}).")
        taken_ids = {held[i].id for i in taken}
        lines = [{"pay_line_id": str(pl.id), "position": pl.position, "name": pl.beneficiary_name,
                  "amount": money_str(pl.amount), "currency": pl.currency, "method": pl.method,
                  "mark": "PAID" if pl.id in taken_ids else "HELD"} for pl in held]
        total_paid = round_money(sum((costs[i] for i in taken), Decimal("0")))
        still_held_cost = round_money(sum(
            (cost for i, cost in enumerate(costs) if held[i].id not in taken_ids), Decimal("0")))
        residual_after = round_money(max(Decimal("0"), still_held_cost - remaining))
        return {"phase": 2, "lines": lines, "total_usd": money_str(total_paid),
                "available_usd": money_str(available), "residual_after_usd": money_str(residual_after)}

    @staticmethod
    def _residual_snapshot(plan: dict) -> dict:
        """The exact thing a phase-2 approval binds (lines + marks + totals)."""
        return {"phase": plan["phase"], "lines": plan["lines"],
                "total_usd": plan["total_usd"], "residual_after_usd": plan["residual_after_usd"]}

    def prepare_residual_plan(self, s: Session, *, run: PayrollRun, employer: Employer) -> PayrollRun:
        """Phase 2, step 1: compute and store a FRESH residual plan snapshot.

        The original (phase-1) approval never covers the remainder: this
        plan must be approved separately (see :meth:`decide_residual`)
        before :meth:`execute_residual_plan` will move anything.
        """
        if run.status != "PARTIALLY_SETTLED":
            raise RuntimeConflict("A residual plan can only be prepared for a PARTIALLY_SETTLED run.")
        plan = self._compute_residual_plan(s, run, employer)
        snapshot = self._residual_snapshot(plan)
        plan["snapshot_hash"] = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()
        plan["status"] = "AWAITING_APPROVAL"
        plan["prepared_at"] = datetime.now(UTC).isoformat()
        run.residual_plan = plan
        self._append_event(s, run, "assess",
                           f"Residual plan prepared: {sum(1 for l in plan['lines'] if l['mark'] == 'PAID')} "
                           f"held line(s) now fit (USD {plan['total_usd']}); awaiting a fresh approval.")
        s.commit()
        return run

    def _append_event(self, s: Session, run: PayrollRun, agent: str, message: str,
                      level: str = "info") -> None:
        seq = s.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.run_id == run.id)) or 0
        s.add(AgentEvent(run_id=run.id, organization_id=run.organization_id, seq=seq,
                         agent=agent, level=level, message=message))

    def decide_residual(self, s: Session, *, run: PayrollRun, employer: Employer, user: User,
                        decision: str) -> PayrollRun:
        """Phase 2, step 2: TAP-sign the residual snapshot and, on approve, execute it.

        The snapshot is recomputed from CURRENT state and hash-compared with
        the prepared plan: any change to a held line, a priority, or the
        wallet since preparation invalidates the plan (SNAPSHOT_MISMATCH) —
        the same discipline as a phase-1 approval.
        """
        plan = run.residual_plan
        if run.status != "PARTIALLY_SETTLED" or not plan or plan.get("status") != "AWAITING_APPROVAL":
            raise RuntimeConflict("This run has no residual plan waiting for a decision. "
                                  "Prepare a residual plan first.")
        fresh = self._compute_residual_plan(s, run, employer)
        fresh_hash = hashlib.sha256(canonical_json(self._residual_snapshot(fresh)).encode()).hexdigest()
        if fresh_hash != plan.get("snapshot_hash"):
            raise RuntimeConflict(
                "SNAPSHOT_MISMATCH: the held lines or the wallet changed since the residual plan was "
                "prepared. Prepare a fresh residual plan and approve that instead.")
        approval = Approval(run_id=run.id, kind="approval", decision=decision,
                            decided_by_id=user.id, decided_by_email=user.email)
        if decision == "approve":
            body = {"type": "payroll.residual_approval", "run_id": str(run.id),
                    "employer": employer.ledger_key, "decision": decision, "decided_by": user.email,
                    "phase": 2, "snapshot_hash": fresh_hash,
                    "required_usd": plan["total_usd"], "residual_after_usd": plan["residual_after_usd"],
                    "pay_lines": [{"name": l["name"], "amount": l["amount"], "currency": l["currency"],
                                   "method": l["method"]} for l in plan["lines"] if l["mark"] == "PAID"],
                    "held_lines": [{"name": l["name"], "amount": l["amount"], "currency": l["currency"],
                                    "method": l["method"]} for l in plan["lines"] if l["mark"] == "HELD"]}
            envelope = self.signer.sign(f"/runs/{run.id}/approve-residual", body)
            check = self.signer.verify(envelope, body)
            if not check.valid:
                raise RuntimeConflict(f"Residual approval failed TAP verification ({check.code}).")
            sig_input = envelope.headers["Signature-Input"]
            approval.signed_body = envelope.body
            approval.tap_signature_input = sig_input
            approval.tap_signature = envelope.headers["Signature"]
            approval.tap_key_id = _keyword(sig_input, "keyid")
            approval.tap_nonce = _keyword(sig_input, "nonce")
            approval.verified = True
            s.add(approval)
            plan = {**plan, "status": "APPROVED"}
            run.residual_plan = plan
            s.flush()
            return self.execute_residual_plan(s, run=run, employer=employer)
        approval.verified = False
        s.add(approval)
        run.residual_plan = {**plan, "status": "HELD_BY_HUMAN"}
        self._append_event(s, run, "approval",
                           f"{user.email} held the residual plan; held lines stay HELD.", "warning")
        s.commit()
        return run

    def execute_residual_plan(self, s: Session, *, run: PayrollRun, employer: Employer) -> PayrollRun:
        """Phase 2, step 3: settle the residual plan's PAID-marked lines.

        Refuses unless the CURRENT residual plan carries a fresh approval —
        the phase-1 approval never covers the remainder. Journals post only
        for the lines settled here; no second platform fee is charged.
        Idempotency: each line pays through its stored ``request_id``.
        """
        plan = run.residual_plan
        if run.status != "PARTIALLY_SETTLED" or not plan or plan.get("status") != "APPROVED":
            raise RuntimeConflict(
                "Residual execution refused: the held lines need a FRESH residual-plan approval first — "
                "the original run approval does not cover them.")
        acct = employer.airwallex_account_id
        key = employer.ledger_key
        by_id = {pl.id: pl for pl in run.pay_lines}
        paid_marks = [l for l in plan["lines"] if l["mark"] == "PAID"]
        ordered = sorted(paid_marks, key=lambda l: l["position"])
        with self.lock:
            # FX first, per currency: convert exactly the priced requirement
            # (salaries + payout fees) for the lines about to settle.
            by_ccy: dict[str, Decimal] = {}
            for mark in ordered:
                pl = by_id[uuid.UUID(mark["pay_line_id"])]
                fee = PAYROLL_POLICY.payout_fee(pl.currency, pl.method)
                by_ccy[pl.currency] = by_ccy.get(pl.currency, Decimal("0")) + pl.amount + fee
            for ccy, required in sorted(by_ccy.items()):
                if ccy == "USD":
                    continue
                required = round_money(required)
                quote = self.recorder.create_fx_quote(
                    request_id=request_id_for(str(run.id), f"rescue-fx-quote-{ccy}"), on_behalf_of=acct,
                    sell_currency="USD", buy_currency=ccy, buy_amount=required)
                conversion = self.recorder.create_fx_conversion(
                    request_id=request_id_for(str(run.id), f"rescue-fx-{ccy}"), on_behalf_of=acct,
                    quote_id=quote["id"], buy_amount=required, sell_currency="USD", buy_currency=ccy)
                sell = round_money(to_decimal(str(conversion["sell_amount"])))
                self.ledger.post([(f"{key}:cash:USD", -sell), (f"{key}:fx_clearing:USD", sell),
                                  (f"{key}:fx_clearing:{ccy}", -required), (f"{key}:cash:{ccy}", required)],
                                 memo=f"{run.id} rescue FX USD->{ccy}")
            for mark in ordered:
                pl = by_id[uuid.UUID(mark["pay_line_id"])]
                if pl.status == "PAID":
                    continue  # idempotent replay: this line already settled
                contractor = s.get(Contractor, pl.contractor_id) if pl.contractor_id else None
                bene = self.recorder.create_beneficiary(
                    request_id=request_id_for(str(run.id), f"rescue-beneficiary-{pl.position}"),
                    on_behalf_of=acct, name=pl.beneficiary_name, currency=pl.currency, method=pl.method,
                    country_code=contractor.country if contractor else "DE",
                    iban=contractor.iban if contractor else "", swift_code=contractor.swift_code if contractor else "",
                    account_number=contractor.account_number if contractor else "",
                    routing_code=contractor.routing_code if contractor else "")
                transfer = self.recorder.create_transfer(
                    request_id=pl.request_id, on_behalf_of=acct, beneficiary_id=bene["id"],
                    amount=pl.amount, currency=pl.currency, method=pl.method)
                self.recorder.transition_transfer(
                    request_id=request_id_for(str(run.id), f"rescue-pay-{pl.position}-transition"),
                    on_behalf_of=acct, transfer_id=transfer["id"], target_status="PAID")
                fee = round_money(to_decimal(str(transfer.get("fee_amount", "0"))))
                self.ledger.post([(f"{key}:cash:{pl.currency}", -(pl.amount + fee)),
                                  (f"external:payees:{pl.currency}", pl.amount),
                                  (f"external:payout_fees:{pl.currency}", fee)],
                                 memo=f"{run.id} payout rescue {pl.position} {pl.beneficiary_name}")
                pl.status = "PAID"
                pl.payout_fee = fee
                pl.airwallex_transfer_id = transfer.get("id")
                pl.settled_phase = 2
                pl.settled_at = datetime.now(UTC)
                self._append_event(s, run, "execute",
                                   f"Rescue: paid {pl.beneficiary_name} {pl.currency} "
                                   f"{money_str(pl.amount)} via {pl.method} (phase 2, fresh approval).",
                                   "success")
            self._flush_calls(s, run.organization_id, employer.id, run.id)
            self._flush_ledger(s, {key: (run.organization_id, employer.id)})
            if not self.ledger.is_balanced():
                raise RuntimeError("Ledger trial balance does not sum to zero after residual execution.")
            remaining_held = [pl for pl in run.pay_lines if pl.status == "HELD"]
            run.residual_plan = {**plan, "status": "EXECUTED"}
            if remaining_held:
                # Recompute the residual honestly for the lines still held:
                # their full USD cost minus the spendable balance now left.
                rates = self.services.usd_rates
                held_cost = round_money(sum(
                    (line_cost_usd(ContractorPayroll(name=pl.beneficiary_name, amount=pl.amount,
                                                     currency=pl.currency, method=pl.method),
                                   rates, PAYROLL_POLICY) for pl in remaining_held), Decimal("0")))
                spendable_after = round_money(
                    self._available_usd(s, employer) - PAYROLL_POLICY.reserve_floor_usd)
                run.residual_shortfall_usd = round_money(max(Decimal("0"), held_cost - spendable_after))
                self._append_event(s, run, "reconcile",
                                   f"Residual execution reconciled: {len(remaining_held)} line(s) still "
                                   "HELD; run remains PARTIALLY_SETTLED.", "warning")
            else:
                run.status = "SETTLED"
                run.residual_shortfall_usd = Decimal("0.00")
                run.settled_at = datetime.now(UTC)
                self._append_event(s, run, "reconcile",
                                   "Residual execution reconciled: every line is now PAID; run SETTLED "
                                   "in two phases.", "success")
            s.commit()
        return run
