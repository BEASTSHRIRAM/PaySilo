"""Graph-level safety: TAP-signed approvals, refusals, fee/reserve, multi-currency."""

from decimal import Decimal

from langgraph.types import Command

from app.agents.demo import build_demo_services
from app.agents.graph import build_graph, initial_state

EUR_LINE = {"name": "Lena Fischer", "amount": "3000", "currency": "EUR", "method": "SWIFT",
            "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX"}


def _start(funding: str, lines: list[dict], run_id: str = "run-x"):
    services = build_demo_services()
    graph = build_graph(services)
    config = {"configurable": {"thread_id": run_id}}
    result = graph.invoke(initial_state(run_id=run_id, employer_key="acme", employer_name="Acme",
                                        funding_usd=funding, pay_lines=lines), config)
    return services, graph, config, result


def _signed(services, graph, config, decision="approve", tamper=None):
    state = graph.get_state(config).values
    pay_lines = state["pay_lines"] if tamper is None else tamper(state["pay_lines"])
    envelope = services.tap.sign_approval(run_id=state["run_id"], employer_key=state["employer_key"],
                                          decision=decision, assessment=state["assessment"],
                                          pay_lines=pay_lines, decided_by="ana@acme.example")
    return {"decision": decision, "decided_by": "ana@acme.example", "tap": envelope.as_dict()}


def test_signed_approval_settles_and_keeps_reserve():
    services, graph, config, result = _start("20000", [EUR_LINE])
    assert result["__interrupt__"][0].value["kind"] == "approval"
    graph.invoke(Command(resume=_signed(services, graph, config)), config)
    state = graph.get_state(config).values
    assert state["status"] == "SETTLED"
    assert any(s["kind"] == "approval" for s in state["signatures"])
    assert any(s["kind"] == "payout" for s in state["signatures"])
    # 20000 - (3012.85 EUR * 1.08 = 3253.88) - 149 fee
    assert Decimal(state["balances"]["USD"]) == Decimal("16597.12")
    assert services.ledger.is_balanced()


def test_unsigned_approval_moves_no_money():
    services, graph, config, _ = _start("20000", [EUR_LINE])
    graph.invoke(Command(resume={"decision": "approve", "decided_by": "mallory"}), config)
    state = graph.get_state(config).values
    assert state["status"] == "APPROVAL_REJECTED"
    assert state["balances"]["USD"] == "20000.00"
    assert services.airwallex.balance_of("USD") == Decimal("0.00")  # no fee charged


def test_approval_signed_over_different_amounts_is_rejected():
    def inflate(lines):
        return [{**l, "amount": "9999.00"} for l in lines]

    services, graph, config, _ = _start("20000", [EUR_LINE])
    graph.invoke(Command(resume=_signed(services, graph, config, tamper=inflate)), config)
    state = graph.get_state(config).values
    assert state["status"] == "APPROVAL_REJECTED"
    assert "body_mismatch" in state["error"]


def test_replayed_approval_is_rejected():
    services, graph, config, _ = _start("20000", [EUR_LINE], run_id="run-a")
    resume = _signed(services, graph, config)
    graph.invoke(Command(resume=resume), config)
    assert graph.get_state(config).values["status"] == "SETTLED"
    # The same signed envelope presented again for an identical run is refused.
    config_b = {"configurable": {"thread_id": "run-a-again"}}
    graph.invoke(initial_state(run_id="run-a", employer_key="acme", employer_name="Acme",
                               funding_usd="0", pay_lines=[EUR_LINE],
                               account_id=graph.get_state(config).values["account_id"]), config_b)
    graph.invoke(Command(resume=resume), config_b)
    assert graph.get_state(config_b).values["status"] == "APPROVAL_REJECTED"


def test_hold_decision_moves_no_money():
    services, graph, config, _ = _start("20000", [EUR_LINE])
    graph.invoke(Command(resume=_signed(services, graph, config, decision="hold")), config)
    state = graph.get_state(config).values
    assert state["status"] == "HELD_BY_HUMAN"
    assert state["balances"]["USD"] == "20000.00"


def test_payroll_that_cannot_afford_fee_and_reserve_is_escalated_not_paid():
    _, graph, config, result = _start("3253.88", [EUR_LINE])
    assert result["__interrupt__"][0].value["kind"] == "escalation"
    graph.invoke(Command(resume={"decision": "hold", "decided_by": "ops"}), config)
    state = graph.get_state(config).values
    assert state["status"] == "HELD_SHORTFALL"
    assert state["balances"]["USD"] == "3253.88"


def test_missing_bank_details_is_refused():
    line = {k: v for k, v in EUR_LINE.items() if k not in ("iban", "swift_code")}
    _, graph, config, result = _start("20000", [line])
    assert "__interrupt__" not in result
    state = graph.get_state(config).values
    assert state["status"] == "REFUSED"
    assert "bank details" in state["error"]


def test_multi_currency_run_settles_and_reconciles():
    lines = [EUR_LINE, {"name": "Sam Lee", "amount": "500", "currency": "USD", "method": "LOCAL",
                        "account_number": "000123456789", "routing_code": "021000021",
                        "country_code": "US"}]
    services, graph, config, _ = _start("20000", lines)
    graph.invoke(Command(resume=_signed(services, graph, config)), config)
    state = graph.get_state(config).values
    assert state["status"] == "SETTLED"
    assert state["assessment"]["payroll_currency"] == "MIXED"
    # 20000 - 3253.88 (EUR leg) - 500 (USD local) - 149 fee
    assert Decimal(state["balances"]["USD"]) == Decimal("16097.12")
    assert services.ledger.is_balanced()


def test_every_event_names_its_agent():
    services, graph, config, _ = _start("20000", [EUR_LINE])
    graph.invoke(Command(resume=_signed(services, graph, config)), config)
    agents = {e["agent"] for e in graph.get_state(config).values["events"]}
    assert {"intake", "price", "assess", "approval", "execute", "fee", "reconcile", "attest"} <= agents
