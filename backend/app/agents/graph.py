"""The PaySilo payroll graph (LangGraph).

One graph invocation = one employer's payroll run. The supervisor is
deterministic code: routing reads the policy assessment, never an LLM.

    intake -> price -> assess -> route
        route = escalate  -> escalate (interrupt: human decision) -> close
        route = approval  -> approval (interrupt: approve / hold)
                                approve -> execute -> fee -> reconcile -> attest -> END
                                hold    -> close
        route = refused   -> close

Human-in-the-loop uses LangGraph ``interrupt`` with a checkpointer, so a
paused run survives and resumes exactly where it stopped. Demo/local runs
use ``MemorySaver``; in production swap in the Postgres checkpointer
(``langgraph-checkpoint-postgres``) with the same graph code — see README.

Idempotency: every money-moving call derives its ``request_id`` from the
run id + operation, in code. Resuming or retrying a node can never pay a
contractor twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..airwallex.simulator import request_id_for
from ..ledger import Ledger
from ..money import money_str, round_money, to_decimal
from ..partners.metal import SettlementChain
from ..policy import PAYROLL_POLICY, ContractorPayroll, RefusalError, assess_payroll
from .intake import explain_assessment, parse_intake
from .state import PayrollRunState


@dataclass
class Services:
    """Everything the graph touches. ``airwallex`` is the simulator in demo
    mode and the real REST client live — the graph cannot tell the difference."""

    airwallex: object  # AirwallexSimulator | AirwallexClient (same interface)
    ledger: Ledger
    chain: SettlementChain
    usd_rates: dict[str, Decimal]


def initial_state(*, run_id: str, employer_key: str, employer_name: str,
                  contact_name: str = "", funding_usd: object = "0",
                  pay_lines: list[dict] | None = None,
                  raw_input: str | None = None) -> PayrollRunState:
    return PayrollRunState(
        run_id=run_id,
        employer_key=employer_key,
        employer_name=employer_name,
        contact_name=contact_name,
        account_id=None,
        funding_usd=money_str(to_decimal(funding_usd)),
        pay_lines=[
            {"name": str(l["name"]), "amount": money_str(to_decimal(l["amount"])),
             "currency": str(l["currency"]).upper()}
            for l in (pay_lines or [])
        ],
        raw_input=raw_input,
        assessment=None,
        route=None,
        decision=None,
        status="RECEIVED",
        audit=[],
        fee_charged_usd="0.00",
        metal_receipt=None,
        explanation=None,
        error=None,
        balances={},
    )


def _audit(state: PayrollRunState, line: str) -> list[str]:
    return list(state.get("audit", [])) + [line]


def _pay_lines(state: PayrollRunState) -> list[ContractorPayroll]:
    return [
        ContractorPayroll(name=l["name"], amount=to_decimal(l["amount"]), currency=l["currency"])
        for l in state["pay_lines"]
    ]


def build_graph(services: Services):
    awx = services.airwallex
    ledger = services.ledger
    chain = services.chain

    # -- nodes ---------------------------------------------------------------
    def intake_node(state: PayrollRunState) -> dict:
        audit = _audit(state, f"[{state['employer_name']}] Intake: reading payroll instruction.")
        pay_lines = state["pay_lines"]
        funding = to_decimal(state["funding_usd"])
        employer_name = state["employer_name"]
        if state.get("raw_input"):
            employer_name, funding, parsed = parse_intake(state["raw_input"])
            pay_lines = [
                {"name": p.name, "amount": money_str(p.amount), "currency": p.currency}
                for p in parsed
            ]
        account = awx.create_connected_account(
            request_id=request_id_for(state["run_id"], "connected-account"),
            business_name=employer_name,
            contact_name=state.get("contact_name", ""),
        )
        audit.append(f"[{employer_name}] Connected account {account['id']} ACTIVE on Airwallex.")
        if funding > 0:
            awx.deposit(
                request_id=request_id_for(state["run_id"], "funding"),
                account_id=account["id"], amount=funding, currency="USD",
            )
            ledger.post(
                [(f"{state['employer_key']}:cash:USD", funding), ("external:bank:USD", -funding)],
                memo=f"{state['run_id']} funding",
            )
            audit.append(f"[{employer_name}] Funded its own wallet with USD {money_str(funding)}.")
        balances = awx.get_balances(account_id=account["id"])
        return {
            "account_id": account["id"], "employer_name": employer_name,
            "funding_usd": money_str(funding), "pay_lines": pay_lines,
            "balances": {c: money_str(v) for c, v in balances.items()}, "audit": audit,
        }

    def price_node(state: PayrollRunState) -> dict:
        audit = _audit(state, f"[{state['employer_name']}] Pricing payroll, fees first.")
        balances = awx.get_balances(account_id=state["account_id"])
        try:
            assessment = assess_payroll(_pay_lines(state), services.usd_rates, balances)
        except RefusalError as exc:
            audit.append(f"[{state['employer_name']}] POLICY REFUSAL: {exc}")
            return {"route": "refused", "status": "REFUSED", "error": str(exc), "audit": audit}
        audit.append(
            f"[{state['employer_name']}] Priced: salaries EUR {money_str(assessment.total_payroll)} "
            f"+ SWIFT fees EUR {money_str(assessment.swift_fees)} = EUR {money_str(assessment.total_required)} "
            f"(~USD {money_str(assessment.required_usd)}) against its own USD {money_str(assessment.available_usd)}."
        )
        return {
            "assessment": {
                "total_payroll": money_str(assessment.total_payroll),
                "swift_fees": money_str(assessment.swift_fees),
                "total_required": money_str(assessment.total_required),
                "available_usd": money_str(assessment.available_usd),
                "required_usd": money_str(assessment.required_usd),
                "can_run": assessment.can_run,
                "shortfall_usd": money_str(assessment.shortfall_usd),
            },
            "status": "PRICED",
            "explanation": explain_assessment(state["employer_name"], assessment),
            "audit": audit,
        }

    def assess_node(state: PayrollRunState) -> dict:
        assessment = state["assessment"]
        if assessment["can_run"]:
            audit = _audit(
                state,
                f"[{state['employer_name']}] Policy: CAN RUN — own funds cover payroll + fees "
                f"(reserve floor USD {money_str(PAYROLL_POLICY.reserve_floor_usd)} respected). "
                "Reserved pending human approval.",
            )
            return {"route": "approval", "status": "RESERVED", "audit": audit}
        audit = _audit(
            state,
            f"[{state['employer_name']}] Policy: SHORTFALL USD {assessment['shortfall_usd']} — "
            "DO NOT RUN this payroll. Escalating to a human.",
        )
        return {"route": "escalate", "status": "RESERVED", "audit": audit}

    def approval_node(state: PayrollRunState) -> dict:
        decision = interrupt({
            "kind": "approval",
            "employer": state["employer_name"],
            "run_id": state["run_id"],
            "total_required_eur": state["assessment"]["total_required"],
            "required_usd": state["assessment"]["required_usd"],
            "pay_lines": state["pay_lines"],
            "message": "Approve this exact payroll? Amount, currency and beneficiaries are fixed.",
        })
        decision = decision.get("decision", "hold") if isinstance(decision, dict) else str(decision)
        audit = _audit(state, f"[{state['employer_name']}] Human decision at approval gate: {decision}.")
        if decision == "approve":
            return {"decision": decision, "status": "APPROVED", "audit": audit}
        return {"decision": decision, "status": "HELD_BY_HUMAN", "audit": audit}

    def escalate_node(state: PayrollRunState) -> dict:
        decision = interrupt({
            "kind": "escalation",
            "employer": state["employer_name"],
            "run_id": state["run_id"],
            "shortfall_usd": state["assessment"]["shortfall_usd"],
            "required_usd": state["assessment"]["required_usd"],
            "available_usd": state["assessment"]["available_usd"],
            "message": "This employer's payroll is short. It stays HELD until its own funds arrive.",
        })
        decision = decision.get("decision", "hold") if isinstance(decision, dict) else str(decision)
        audit = _audit(
            state,
            f"[{state['employer_name']}] Escalation acknowledged by human ({decision}): payroll HELD_SHORTFALL.",
        )
        return {"decision": decision, "status": "HELD_SHORTFALL", "audit": audit}

    def execute_node(state: PayrollRunState) -> dict:
        run_id, key, acct = state["run_id"], state["employer_key"], state["account_id"]
        name = state["employer_name"]
        assessment = state["assessment"]
        total_required = to_decimal(assessment["total_required"])
        audit = _audit(state, f"[{name}] Converting USD -> EUR {money_str(total_required)} on {name}'s behalf (x-on-behalf-of).")
        quote = awx.create_fx_quote(
            request_id=request_id_for(run_id, "fx-quote"), on_behalf_of=acct,
            sell_currency="USD", buy_currency="EUR", buy_amount=total_required,
        )
        conversion = awx.create_fx_conversion(
            request_id=request_id_for(run_id, "fx-conversion"), on_behalf_of=acct,
            quote_id=quote["id"], buy_amount=total_required,
        )
        sell_amount = round_money(to_decimal(conversion["sell_amount"]))
        ledger.post(
            [(f"{key}:cash:USD", -sell_amount), (f"{key}:fx_clearing:USD", sell_amount),
             (f"{key}:fx_clearing:EUR", -total_required), (f"{key}:cash:EUR", total_required)],
            memo=f"{run_id} FX conversion",
        )
        total_fees = Decimal("0")
        for i, line in enumerate(state["pay_lines"]):
            bene = awx.create_beneficiary(
                request_id=request_id_for(run_id, f"beneficiary-{i}"), on_behalf_of=acct,
                name=line["name"], iban="DE89370400440532013000", swift_code="COBADEFFXXX",
            )
            transfer = awx.create_transfer(
                request_id=request_id_for(run_id, f"pay-{i}"), on_behalf_of=acct,
                beneficiary_id=bene["id"], amount=to_decimal(line["amount"]),
                currency=line["currency"], method="SWIFT",
            )
            paid = awx.transition_transfer(
                request_id=request_id_for(run_id, f"pay-{i}-transition"), on_behalf_of=acct,
                transfer_id=transfer["id"], target_status="PAID",
            )
            total_fees += to_decimal(transfer.get("fee_amount", "0"))
            audit.append(f"[{name}] Paid {line['name']} EUR {line['amount']} via SWIFT ({paid['id']} {paid['status']}).")
        ledger.post(
            [(f"{key}:cash:EUR", -total_required),
             ("external:payees:EUR", total_required - total_fees),
             ("external:swift_network:EUR", total_fees)],
            memo=f"{run_id} contractor payouts + SWIFT fees",
        )
        balances = awx.get_balances(account_id=acct)
        return {"status": "SENT", "balances": {c: money_str(v) for c, v in balances.items()}, "audit": audit}

    def fee_node(state: PayrollRunState) -> dict:
        fee = PAYROLL_POLICY.platform_fee_per_run_usd
        awx.create_charge(
            request_id=request_id_for(state["run_id"], "platform-fee"),
            source_account_id=state["account_id"], amount=fee, currency="USD",
            reference="payroll run fee",
        )
        ledger.post(
            [(f"{state['employer_key']}:cash:USD", -fee), ("platform:fees:USD", fee)],
            memo=f"{state['run_id']} platform run fee",
        )
        audit = _audit(
            state,
            f"[{state['employer_name']}] Platform fee USD {money_str(fee)} collected — "
            "charged only because this payroll actually ran.",
        )
        balances = awx.get_balances(account_id=state["account_id"])
        return {"fee_charged_usd": money_str(fee),
                "balances": {c: money_str(v) for c, v in balances.items()}, "audit": audit}

    def reconcile_node(state: PayrollRunState) -> dict:
        key, acct = state["employer_key"], state["account_id"]
        if not ledger.is_balanced():
            raise RuntimeError("Ledger trial balance does not sum to zero — refusing to close the run.")
        for currency in ("USD", "EUR"):
            ledger_cash = ledger.employer_cash(key, currency)
            wallet_cash = awx.balance_of(currency, account_id=acct)
            if ledger_cash != wallet_cash:
                raise RuntimeError(
                    f"Reconciliation mismatch for {state['employer_name']} {currency}: "
                    f"ledger {ledger_cash} != wallet {wallet_cash}"
                )
        audit = _audit(
            state,
            f"[{state['employer_name']}] Reconciled: ledger balances match wallet balances; books balance.",
        )
        return {"status": "SETTLED", "audit": audit}

    def attest_node(state: PayrollRunState) -> dict:
        receipt = chain.attest({
            "run_id": state["run_id"], "employer": state["employer_name"],
            "status": state["status"], "total_required_eur": state["assessment"]["total_required"],
            "fee_charged_usd": state["fee_charged_usd"],
        })
        audit = _audit(
            state,
            f"[{state['employer_name']}] Reconciliation receipt anchored on settlement chain "
            f"({receipt.chain}, receipt #{receipt.index}, hash {receipt.receipt_hash[:16]}…).",
        )
        return {
            "metal_receipt": {"index": receipt.index, "payload_hash": receipt.payload_hash,
                              "prev_hash": receipt.prev_hash, "receipt_hash": receipt.receipt_hash,
                              "chain": receipt.chain},
            "audit": audit,
        }

    def close_node(state: PayrollRunState) -> dict:
        return {}

    # -- routing (deterministic code only) ------------------------------------
    def route_after_assess(state: PayrollRunState) -> str:
        return state["route"] or "refused"

    def route_after_approval(state: PayrollRunState) -> str:
        return "execute" if state.get("decision") == "approve" else "close"

    builder = StateGraph(PayrollRunState)
    builder.add_node("intake", intake_node)
    builder.add_node("price", price_node)
    builder.add_node("assess", assess_node)
    builder.add_node("approval", approval_node)
    builder.add_node("escalate", escalate_node)
    builder.add_node("execute", execute_node)
    builder.add_node("fee", fee_node)
    builder.add_node("reconcile", reconcile_node)
    builder.add_node("attest", attest_node)
    builder.add_node("close", close_node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "price")
    builder.add_edge("price", "assess")
    builder.add_conditional_edges("assess", route_after_assess,
                                  {"approval": "approval", "escalate": "escalate", "refused": "close"})
    builder.add_conditional_edges("approval", route_after_approval,
                                  {"execute": "execute", "close": "close"})
    builder.add_edge("escalate", "close")
    builder.add_edge("execute", "fee")
    builder.add_edge("fee", "reconcile")
    builder.add_edge("reconcile", "attest")
    builder.add_edge("attest", END)
    builder.add_edge("close", END)
    return builder.compile(checkpointer=MemorySaver())
