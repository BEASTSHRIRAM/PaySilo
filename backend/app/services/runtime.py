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
survive restarts. A run paused at an approval lives in the in-process
LangGraph checkpointer; if the server restarts mid-pause the run is marked
not resumable and a new run must be started.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents.graph import Services, build_graph, initial_state
from ..airwallex.client import AirwallexClient
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
from ..partners.metal import MockSettlementChain
from ..partners.tap import generate_tap_agent
from ..partners.tap_signing import TapSigner

logger = logging.getLogger(__name__)

TERMINAL = {"SETTLED", "HELD_BY_HUMAN", "APPROVAL_REJECTED", "REFUSED", "FAILED"}
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
        self.recorder = RecordingAirwallex(
            AirwallexSimulator() if settings.demo_mode else AirwallexClient(settings))
        key = generate_tap_agent("paysilo-agent", key_id=f"paysilo-agent-{uuid.uuid4().hex[:12]}")
        self.signer = TapSigner(key=key)
        self.ledger = Ledger()
        self.chain = MockSettlementChain()
        self.services = Services(airwallex=self.recorder, ledger=self.ledger, chain=self.chain,
                                 usd_rates=dict(USD_VALUE), tap=self.signer)
        self.graph = build_graph(self.services)
        self._live_threads: set[str] = set()
        self._ledger_synced = 0
        self._calls_synced = 0

    # ------------------------------------------------------------------ boot
    def startup(self) -> None:
        with self.db.sessionmaker() as s:
            s.merge(TapKey(key_id=self.signer.key.key_id, agent_id=self.signer.key.agent_id,
                           public_key_pem=self.signer.key.public_key_pem()))
            s.commit()
            self._hydrate(s)

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
                business_name=employer.name, contact_name=employer.contact_name or "")
            employer.airwallex_account_id = account["id"]
            employer.status = "ACTIVE"
            self._flush_calls(s, employer.organization_id, employer.id, None)
        return employer

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

    def balances(self, s: Session, employer: Employer) -> dict[str, str]:
        """Derived from persisted ledger entries — never from a stored balance."""
        rows = s.execute(
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount))
            .where(LedgerEntry.account.like(f"{employer.ledger_key}:cash:%"))
            .group_by(LedgerEntry.currency)
        ).all()
        return {ccy: money_str(total) for ccy, total in rows if total and Decimal(total) != 0}

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
        run = PayrollRun(organization_id=org.id, employer_id=employer.id, created_by_id=user.id,
                         source=source, raw_input=raw_input, period_label=period_label, status="RECEIVED")
        s.add(run)
        s.flush()
        directory = [
            {"name": c.full_name, "currency": c.currency, "method": c.payout_method, "iban": c.iban,
             "swift_code": c.swift_code, "account_number": c.account_number, "routing_code": c.routing_code,
             "country_code": c.country, "contractor_id": str(c.id)}
            for c in employer.contractors if c.is_active
        ]
        state = initial_state(
            run_id=str(run.id), employer_key=employer.ledger_key, employer_name=employer.name,
            contact_name=employer.contact_name or "", funding_usd="0", pay_lines=pay_lines or [],
            raw_input=raw_input, account_id=employer.airwallex_account_id, directory=directory)
        with self.lock:
            config = {"configurable": {"thread_id": str(run.id)}}
            result = self.graph.invoke(state, config)
            self._live_threads.add(str(run.id))
            self._sync(s, run, employer, result)
        s.commit()
        return run

    def decide(self, s: Session, *, run: PayrollRun, employer: Employer, user: User, decision: str) -> PayrollRun:
        if run.pending_interrupt is None:
            raise RuntimeConflict("This run is not waiting for a decision.")
        if str(run.id) not in self._live_threads:
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
                    assessment=state["assessment"], pay_lines=state["pay_lines"], decided_by=user.email)
                resume["tap"] = envelope.as_dict()
                sig_input = envelope.headers["Signature-Input"]
                approval.signed_body = envelope.body
                approval.tap_signature_input = sig_input
                approval.tap_signature = envelope.headers["Signature"]
                approval.tap_key_id = _keyword(sig_input, "keyid")
                approval.tap_nonce = _keyword(sig_input, "nonce")
            result = self.graph.invoke(Command(resume=resume), config)
            self._sync(s, run, employer, result)
            approval.verified = kind == "approval" and run.status != "APPROVAL_REJECTED"
            s.add(approval)
        s.commit()
        return run

    def is_resumable(self, run: PayrollRun) -> bool:
        return run.pending_interrupt is not None and str(run.id) in self._live_threads

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
            run.shortfall_usd = to_decimal(a["shortfall_usd"])
        run.pending_interrupt = result["__interrupt__"][0].value if "__interrupt__" in result else None
        if run.status == "SETTLED" and run.settled_at is None:
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
                                    receipt_hash=receipt["receipt_hash"]))
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
