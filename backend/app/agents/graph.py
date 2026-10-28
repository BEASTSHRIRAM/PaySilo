"""The PaySilo payroll graph (LangGraph).

One graph invocation = one employer's payroll run. The supervisor is
deterministic code: routing reads the policy assessment, never an LLM.

    intake -> price -> assess -> route
        route = escalate  -> escalate (interrupt: human decision) -> close
        route = approval  -> approval (interrupt: TAP-signed approve / hold)
                                approve -> execute -> fee -> reconcile -> attest -> END
                                hold / bad signature -> close
        route = refused   -> close

Each node is one specialised agent with one job (see ``AGENTS``). Every
node appends structured events to ``state["events"]`` (agent, level,
message) and a flat line to ``state["audit"]`` so the dashboard and the
audit trail replay exactly what happened.

Human-in-the-loop uses LangGraph ``interrupt`` with a checkpointer, so a
paused run survives and resumes exactly where it stopped. ``MemorySaver``
is the default; pass any LangGraph checkpointer (e.g. the Postgres one) to
``build_graph`` for durable runs.

Visa TAP: an approval only counts if it carries a TAP signature over the
exact run, employer, amounts and pay lines held in graph state; every
payout instruction is signed and verified right before the transfer.

Idempotency: every money-moving call derives its ``request_id`` from the
run id + operation, in code. Resuming or retrying a node can never pay a
contractor twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..airwallex.simulator import request_id_for
from ..ledger import Ledger
from ..money import money_str, round_money, to_decimal
from ..partners.metal import SettlementChain
from ..partners.tap_signing import SignedEnvelope, TapSigner, approval_body
from ..policy import PAYROLL_POLICY, ContractorPayroll, PayrollAssessment, RefusalError, assess_payroll
from .intake import explain_assessment, parse_intake
from .state import PayrollRunState

# The agents of the PaySilo crew, keyed by graph node. Only "intake" uses an LLM.
AGENTS: dict[str, dict[str, str]] = {
    "intake": {"name": "Intake Agent", "kind": "llm",
               "job": "Reads payroll sheets and emails into structured pay lines (Claude)."},
    "price": {"name": "Pricing Agent", "kind": "code",
              "job": "Prices salaries plus payout fees per currency, before any FX."},
    "assess": {"name": "Balance & Policy Agent", "kind": "code",
               "job": "Checks only this employer's wallet against payroll, run fee and reserve floor."},
    "approval": {"name": "Approval Gate", "kind": "human",
                 "job": "Pauses for a Visa TAP-signed human decision on the exact amounts."},
    "escalate": {"name": "Escalation", "kind": "human",
                 "job": "Holds a short payroll and asks a human, never borrowing from another employer."},
    "execute": {"name": "FX & Payout Agent", "kind": "tools",
                "job": "Converts and pays on the employer's behalf (x-on-behalf-of), TAP-signed payouts."},
    "fee": {"name": "Fee Agent", "kind": "tools",
            "job": "Collects the platform run fee only from payrolls that actually ran."},
    "reconcile": {"name": "Reconciliation Agent", "kind": "code",
                  "job": "Proves the ledger matches the wallet and the books balance."},
    "attest": {"name": "Attestation Agent", "kind": "tools",
               "job": "Anchors a hash of the reconciliation receipt on the settlement chain (Metal)."},
}

PAYOUT_ERRORS: tuple[type[BaseException], ...] = (Exception,)


@dataclass
class Services:
    """Everything the graph touches. ``airwallex`` is the simulator in demo
    mode and the real REST client live — the graph cannot tell the difference."""

    airwallex: object  # AirwallexSimulator | AirwallexClient (same interface)
    ledger: Ledger
    chain: SettlementChain
    usd_rates: dict[str, Decimal]
    tap: TapSigner = field(default_factory=TapSigner)


def _normalise_line(line: dict) -> dict:
    out = {
        "name": str(line["name"]).strip(),
        "amount": money_str(to_decimal(str(line["amount"]))),
        "currency": str(line["currency"]).upper(),
        "method": str(line.get("method") or "SWIFT").upper(),
    }
    for key in ("iban", "swift_code", "account_number", "routing_code", "country_code", "contractor_id"):
        if line.get(key):
            out[key] = str(line[key])
    return out


def _directory_entry(contractor: dict) -> dict:
    keep = ("name", "currency", "method", "iban", "swift_code", "account_number", "routing_code",
            "country_code", "contractor_id")
    out = {k: str(contractor[k]) for k in keep if contractor.get(k)}
    out["name"] = out["name"].strip()
    return out


def initial_state(*, run_id: str, employer_key: str, employer_name: str,
                  contact_name: str = "", funding_usd: object = "0",
                  pay_lines: list[dict] | None = None,
                  raw_input: str | None = None,
                  account_id: str | None = None,
                  directory: list[dict] | None = None) -> PayrollRunState:
    return PayrollRunState(
        run_id=run_id,
        employer_key=employer_key,
        employer_name=employer_name,
        contact_name=contact_name,
        account_id=account_id,
        funding_usd=money_str(to_decimal(str(funding_usd))),
        pay_lines=[_normalise_line(l) for l in (pay_lines or [])],
        raw_input=raw_input,
        directory=[_directory_entry(d) for d in (directory or [])],
        assessment=None,
        route=None,
        decision=None,
        decided_by=None,
        status="RECEIVED",
        audit=[],
        events=[],
        fee_charged_usd="0.00",
        metal_receipt=None,
        explanation=None,
        error=None,
        balances={},
        signatures=[],
    )


def _log(state: PayrollRunState, node: str, message: str, level: str = "info",
         audit: list[str] | None = None, events: list[dict] | None = None) -> tuple[list[str], list[dict]]:
    audit = list(state.get("audit", [])) if audit is None else audit
    events = list(state.get("events", [])) if events is None else events
    audit.append(f"[{state['employer_name']}] {message}")
    events.append({"agent": node, "agent_name": AGENTS.get(node, {}).get("name", node),
                   "level": level, "message": message})
    return audit, events


def _pay_lines(state: PayrollRunState) -> list[ContractorPayroll]:
    return [
        ContractorPayroll(name=l["name"], amount=to_decimal(l["amount"]),
                          currency=l["currency"], method=l.get("method", "SWIFT"))
        for l in state["pay_lines"]
    ]


def assessment_dict(a: PayrollAssessment) -> dict:
    return {
        "payroll_currency": a.payroll_currency,
        "by_currency": [
            {"currency": r.currency, "payouts": r.payouts, "payroll": money_str(r.payroll),
             "fees": money_str(r.fees), "required": money_str(r.required),
             "required_usd": money_str(r.required_usd)}
            for r in a.by_currency
        ],
        "total_payroll": money_str(a.total_payroll),
        "swift_fees": money_str(a.swift_fees),
        "total_required": money_str(a.total_required),
        "payroll_usd": money_str(a.payroll_usd),
        "platform_fee_usd": money_str(a.platform_fee_usd),
        "reserve_floor_usd": money_str(a.reserve_floor_usd),
        "required_usd": money_str(a.required_usd),
        "available_usd": money_str(a.available_usd),
        "shortfall_usd": money_str(a.shortfall_usd),
        "can_run": a.can_run,
    }


def _decision_of(value: object) -> tuple[str, str, dict | None]:
    """Normalise an interrupt resume value -> (decision, decided_by, tap envelope)."""
    if isinstance(value, dict):
        return (str(value.get("decision", "hold")), str(value.get("decided_by", "unknown")),
                value.get("tap"))
    return str(value), "unknown", None


def build_graph(services: Services, checkpointer=None):
    awx = services.airwallex
    ledger = services.ledger
    chain = services.chain
    tap = services.tap

    def balances_str(account_id: str) -> dict[str, str]:
        return {c: money_str(v) for c, v in awx.get_balances(account_id=account_id).items()}

    # -- nodes ---------------------------------------------------------------
    def intake_node(state: PayrollRunState) -> dict:
        audit, events = _log(state, "intake", "Reading the payroll instruction.")
        pay_lines = state["pay_lines"]
        funding = to_decimal(state["funding_usd"])
        employer_name = state["employer_name"]
        if state.get("raw_input"):
            parsed_name, funding, parsed = parse_intake(state["raw_input"])
            if parsed_name and parsed_name != "Unknown Employer":
                employer_name = parsed_name
            # Match names against THIS employer's contractor directory only.
            directory = {d["name"].casefold(): d for d in state.get("directory", [])}
            pay_lines = []
            for p in parsed:
                known = directory.get(p.name.strip().casefold())
                line = {"name": p.name, "amount": p.amount, "currency": p.currency, "method": p.method}
                if known:
                    line.update({k: v for k, v in known.items() if k not in ("name", "amount")})
                    line["currency"] = p.currency
                else:
                    _log(state, "intake", f"{p.name} is not in {employer_name}'s contractor directory.",
                         "warning", audit, events)
                pay_lines.append(_normalise_line(line))
            _log(state, "intake", f"Read {len(pay_lines)} pay line(s) from the instruction.",
                 audit=audit, events=events)
        account_id = state.get("account_id")
        if not account_id:
            account = awx.create_connected_account(
                request_id=request_id_for(state["run_id"], "connected-account"),
                business_name=employer_name, contact_name=state.get("contact_name", ""),
            )
            account_id = account["id"]
            _log(state, "intake", f"Connected account {account_id} ACTIVE on Airwallex.",
                 audit=audit, events=events)
        if funding > 0:
            awx.deposit(request_id=request_id_for(state["run_id"], "funding"),
                        account_id=account_id, amount=funding, currency="USD")
            ledger.post([(f"{state['employer_key']}:cash:USD", funding), ("external:bank:USD", -funding)],
                        memo=f"{state['run_id']} funding")
            _log(state, "intake", f"Funded its own wallet with USD {money_str(funding)}.",
                 audit=audit, events=events)
        return {"account_id": account_id, "employer_name": employer_name,
                "funding_usd": money_str(funding), "pay_lines": pay_lines,
                "balances": balances_str(account_id), "audit": audit, "events": events}

    def price_node(state: PayrollRunState) -> dict:
        audit, events = _log(state, "price", "Pricing payroll, fees first.")
        if not state["pay_lines"]:
            msg = "Nothing to pay: this run has no pay lines."
            _log(state, "price", f"POLICY REFUSAL: {msg}", "error", audit, events)
            return {"route": "refused", "status": "REFUSED", "error": msg, "audit": audit, "events": events}
        missing = [l["name"] for l in state["pay_lines"] if not (l.get("iban") or l.get("account_number"))]
        if missing:
            msg = f"Missing bank details for {', '.join(missing)}; refusing to pay an unknown account."
            _log(state, "price", f"POLICY REFUSAL: {msg}", "error", audit, events)
            return {"route": "refused", "status": "REFUSED", "error": msg, "audit": audit, "events": events}
        balances = awx.get_balances(account_id=state["account_id"])
        try:
            assessment = assess_payroll(_pay_lines(state), services.usd_rates, balances)
        except RefusalError as exc:
            _log(state, "price", f"POLICY REFUSAL: {exc}", "error", audit, events)
            return {"route": "refused", "status": "REFUSED", "error": str(exc), "audit": audit, "events": events}
        for r in assessment.by_currency:
            _log(state, "price",
                 f"Priced {r.currency}: salaries {money_str(r.payroll)} + payout fees {money_str(r.fees)} "
                 f"= {r.currency} {money_str(r.required)} (~USD {money_str(r.required_usd)}).",
                 audit=audit, events=events)
        _log(state, "price",
             f"Total leaving the wallet: USD {money_str(assessment.required_usd)} incl. USD "
             f"{money_str(assessment.platform_fee_usd)} run fee; reserve floor USD "
             f"{money_str(assessment.reserve_floor_usd)} must stay untouched.",
             audit=audit, events=events)
        return {"assessment": assessment_dict(assessment), "status": "PRICED",
                "explanation": explain_assessment(state["employer_name"], assessment),
                "audit": audit, "events": events}

    def assess_node(state: PayrollRunState) -> dict:
        a = state["assessment"]
        if a["can_run"]:
            audit, events = _log(
                state, "assess",
                f"CAN RUN — its own USD {a['available_usd']} covers USD {a['required_usd']} and keeps the "
                f"USD {a['reserve_floor_usd']} reserve. Reserved pending a signed human approval.", "success")
            return {"route": "approval", "status": "RESERVED", "audit": audit, "events": events}
        audit, events = _log(
            state, "assess",
            f"SHORTFALL USD {a['shortfall_usd']} — DO NOT RUN this payroll. Only this employer's wallet "
            "was checked; escalating to a human.", "warning")
        return {"route": "escalate", "status": "HELD_SHORTFALL", "audit": audit, "events": events}

    def approval_node(state: PayrollRunState) -> dict:
        value = interrupt({
            "kind": "approval",
            "employer": state["employer_name"],
            "run_id": state["run_id"],
            "assessment": state["assessment"],
            "pay_lines": state["pay_lines"],
            "message": "Approve this exact payroll? Amounts, currencies and beneficiaries are fixed "
                       "and your decision is Visa TAP-signed.",
        })
        decision, decided_by, envelope = _decision_of(value)
        expected = approval_body(run_id=state["run_id"], employer_key=state["employer_key"],
                                 decision=decision, assessment=state["assessment"],
                                 pay_lines=state["pay_lines"], decided_by=decided_by)
        if envelope is None:
            result_code, ok = "missing_signature", False
        else:
            result = tap.verify(SignedEnvelope.from_dict(envelope), expected)
            result_code, ok = result.code, result.valid
        if not ok:
            audit, events = _log(state, "approval",
                                 f"Approval REJECTED: Visa TAP verification failed ({result_code}). No money moved.",
                                 "error")
            return {"decision": "rejected", "decided_by": decided_by, "status": "APPROVAL_REJECTED",
                    "error": f"TAP verification failed: {result_code}", "audit": audit, "events": events}
        signatures = list(state.get("signatures", [])) + [
            {"kind": "approval", "decided_by": decided_by, "decision": decision,
             "signature_input": envelope["headers"]["Signature-Input"],
             "signature": envelope["headers"]["Signature"]}]
        audit, events = _log(state, "approval",
                             f"{decided_by} decided '{decision}'; Visa TAP signature verified.",
                             "success" if decision == "approve" else "warning")
        status = "APPROVED" if decision == "approve" else "HELD_BY_HUMAN"
        return {"decision": decision, "decided_by": decided_by, "status": status,
                "signatures": signatures, "audit": audit, "events": events}

    def escalate_node(state: PayrollRunState) -> dict:
        a = state["assessment"]
        value = interrupt({
            "kind": "escalation",
            "employer": state["employer_name"],
            "run_id": state["run_id"],
            "assessment": a,
            "shortfall_usd": a["shortfall_usd"],
            "options": ["Ask the employer to top up its own wallet", "Hold until funds arrive",
                        "Reduce or split this payroll"],
            "message": "This employer's payroll is short. It stays HELD until its own funds arrive.",
        })
        decision, decided_by, _ = _decision_of(value)
        audit, events = _log(state, "escalate",
                             f"Escalation acknowledged by {decided_by} ({decision}); payroll stays HELD_SHORTFALL.",
                             "warning")
        return {"decision": decision, "decided_by": decided_by, "status": "HELD_SHORTFALL",
                "audit": audit, "events": events}

    def execute_node(state: PayrollRunState) -> dict:
        run_id, key, acct = state["run_id"], state["employer_key"], state["account_id"]
        a = state["assessment"]
        audit, events = list(state.get("audit", [])), list(state.get("events", []))
        signatures = list(state.get("signatures", []))
        try:
            # 1. Convert exactly the priced amount per currency, on the employer's behalf.
            for req in a["by_currency"]:
                ccy, required = req["currency"], to_decimal(req["required"])
                if ccy == "USD":
                    continue
                quote = awx.create_fx_quote(
                    request_id=request_id_for(run_id, f"fx-quote-{ccy}"), on_behalf_of=acct,
                    sell_currency="USD", buy_currency=ccy, buy_amount=required)
                conversion = awx.create_fx_conversion(
                    request_id=request_id_for(run_id, f"fx-conversion-{ccy}"), on_behalf_of=acct,
                    quote_id=quote["id"], buy_amount=required, sell_currency="USD", buy_currency=ccy)
                sell = round_money(to_decimal(str(conversion["sell_amount"])))
                ledger.post([(f"{key}:cash:USD", -sell), (f"{key}:fx_clearing:USD", sell),
                             (f"{key}:fx_clearing:{ccy}", -required), (f"{key}:cash:{ccy}", required)],
                            memo=f"{run_id} FX USD->{ccy}")
                _log(state, "execute", f"Converted USD {money_str(sell)} -> {ccy} {money_str(required)} "
                     f"on its own behalf (x-on-behalf-of {acct}).", audit=audit, events=events)

            # 2. Pay each contractor with a TAP-signed, verified instruction.
            for i, line in enumerate(state["pay_lines"]):
                envelope = tap.sign_payout(run_id=run_id, employer_key=key, account_id=acct, line=line, index=i)
                check = tap.verify(envelope, {"type": "payroll.payout", "run_id": run_id, "employer": key,
                                              "on_behalf_of": acct, "index": i, "beneficiary": line["name"],
                                              "amount": line["amount"], "currency": line["currency"],
                                              "method": line.get("method", "SWIFT")})
                if not check.valid:
                    raise RuntimeError(f"Payout instruction for {line['name']} failed TAP verification ({check.code})")
                signatures.append({"kind": "payout", "index": i, "beneficiary": line["name"],
                                   "signature_input": envelope.headers["Signature-Input"],
                                   "signature": envelope.headers["Signature"]})
                bene = awx.create_beneficiary(
                    request_id=request_id_for(run_id, f"beneficiary-{i}"), on_behalf_of=acct,
                    name=line["name"], currency=line["currency"], method=line.get("method", "SWIFT"),
                    country_code=line.get("country_code", "DE"), iban=line.get("iban", ""),
                    swift_code=line.get("swift_code", ""), account_number=line.get("account_number", ""),
                    routing_code=line.get("routing_code", ""))
                transfer = awx.create_transfer(
                    request_id=request_id_for(run_id, f"pay-{i}"), on_behalf_of=acct,
                    beneficiary_id=bene["id"], amount=to_decimal(line["amount"]),
                    currency=line["currency"], method=line.get("method", "SWIFT"))
                paid = awx.transition_transfer(
                    request_id=request_id_for(run_id, f"pay-{i}-transition"), on_behalf_of=acct,
                    transfer_id=transfer["id"], target_status="PAID")
                fee = round_money(to_decimal(str(transfer.get("fee_amount", "0"))))
                amount, ccy = to_decimal(line["amount"]), line["currency"]
                # Post per payout so a mid-batch failure leaves the ledger matching the wallet.
                ledger.post([(f"{key}:cash:{ccy}", -(amount + fee)),
                             (f"external:payees:{ccy}", amount),
                             (f"external:payout_fees:{ccy}", fee)],
                            memo=f"{run_id} payout {i} {line['name']}")
                _log(state, "execute",
                     f"Paid {line['name']} {line['currency']} {line['amount']} via {line.get('method', 'SWIFT')} "
                     f"({paid['id']} {paid['status']}, TAP-signed).", "success", audit, events)

        except PAYOUT_ERRORS as exc:
            _log(state, "execute",
                 f"Payout step FAILED: {exc}. Retrying is safe — every call reuses its request_id.",
                 "error", audit, events)
            return {"status": "FAILED", "error": str(exc), "signatures": signatures,
                    "balances": balances_str(acct), "audit": audit, "events": events}
        return {"status": "SENT", "signatures": signatures, "balances": balances_str(acct),
                "audit": audit, "events": events}

    def fee_node(state: PayrollRunState) -> dict:
        fee = to_decimal(state["assessment"]["platform_fee_usd"])
        awx.create_charge(request_id=request_id_for(state["run_id"], "platform-fee"),
                          source_account_id=state["account_id"], amount=fee, currency="USD",
                          reference="payroll run fee")
        ledger.post([(f"{state['employer_key']}:cash:USD", -fee), ("platform:fees:USD", fee)],
                    memo=f"{state['run_id']} platform run fee")
        audit, events = _log(state, "fee", f"Platform fee USD {money_str(fee)} collected — charged only "
                             "because this payroll actually ran.", "success")
        return {"fee_charged_usd": money_str(fee), "balances": balances_str(state["account_id"]),
                "audit": audit, "events": events}

    def reconcile_node(state: PayrollRunState) -> dict:
        key, acct = state["employer_key"], state["account_id"]
        if not ledger.is_balanced():
            raise RuntimeError("Ledger trial balance does not sum to zero — refusing to close the run.")
        wallet = awx.get_balances(account_id=acct)
        currencies = set(wallet) | {r["currency"] for r in state["assessment"]["by_currency"]} | {"USD"}
        for currency in sorted(currencies):
            ledger_cash = ledger.employer_cash(key, currency)
            wallet_cash = wallet.get(currency, Decimal("0.00"))
            if ledger_cash != wallet_cash:
                raise RuntimeError(f"Reconciliation mismatch for {state['employer_name']} {currency}: "
                                   f"ledger {ledger_cash} != wallet {wallet_cash}")
        audit, events = _log(state, "reconcile",
                             "Reconciled: ledger matches the wallet in every currency; books balance.", "success")
        return {"status": "SETTLED", "audit": audit, "events": events}

    def attest_node(state: PayrollRunState) -> dict:
        receipt = chain.attest({
            "run_id": state["run_id"], "employer": state["employer_name"], "status": state["status"],
            "assessment": state["assessment"], "fee_charged_usd": state["fee_charged_usd"],
        })
        audit, events = _log(state, "attest",
                             f"Reconciliation receipt anchored ({receipt.chain}, receipt #{receipt.index}, "
                             f"hash {receipt.receipt_hash[:16]}…).", "success")
        return {"metal_receipt": {"index": receipt.index, "payload_hash": receipt.payload_hash,
                                  "prev_hash": receipt.prev_hash, "receipt_hash": receipt.receipt_hash,
                                  "chain": receipt.chain},
                "audit": audit, "events": events}

    def close_node(state: PayrollRunState) -> dict:
        return {}

    # -- routing (deterministic code only) ------------------------------------
    def route_after_assess(state: PayrollRunState) -> str:
        return state.get("route") or "refused"

    def route_after_approval(state: PayrollRunState) -> str:
        return "execute" if state.get("status") == "APPROVED" else "close"

    def route_after_execute(state: PayrollRunState) -> str:
        return "fee" if state.get("status") == "SENT" else "close"

    builder = StateGraph(PayrollRunState)
    for name, fn in [("intake", intake_node), ("price", price_node), ("assess", assess_node),
                     ("approval", approval_node), ("escalate", escalate_node), ("execute", execute_node),
                     ("fee", fee_node), ("reconcile", reconcile_node), ("attest", attest_node),
                     ("close", close_node)]:
        builder.add_node(name, fn)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "price")
    builder.add_conditional_edges("price", lambda s: "close" if s.get("status") == "REFUSED" else "assess",
                                  {"assess": "assess", "close": "close"})
    builder.add_conditional_edges("assess", route_after_assess,
                                  {"approval": "approval", "escalate": "escalate", "refused": "close"})
    builder.add_conditional_edges("approval", route_after_approval, {"execute": "execute", "close": "close"})
    builder.add_edge("escalate", "close")
    builder.add_conditional_edges("execute", route_after_execute, {"fee": "fee", "close": "close"})
    builder.add_edge("fee", "reconcile")
    builder.add_edge("reconcile", "attest")
    builder.add_edge("attest", END)
    builder.add_edge("close", END)
    return builder.compile(checkpointer=checkpointer or MemorySaver())
