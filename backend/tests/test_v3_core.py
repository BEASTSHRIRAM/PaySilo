"""v3 core fixes: partial payout, post-payout failure persistence, execute re-check,
one open run per employer."""

from __future__ import annotations

from decimal import Decimal

import pytest
from langgraph.types import Command

from app.agents.demo import EMPLOYERS, build_demo_services, demo_resume
from app.agents.graph import build_graph, initial_state
from app.policy import ContractorPayroll, plan_partial_payout

ACME_LINES = EMPLOYERS["acme"]["pay_lines"]
RATES = build_demo_services().usd_rates


def _run_to_interrupt(services, *, funding, lines, run_id="r1", key="acme", name="Acme Manufacturing GmbH"):
    graph = build_graph(services)
    config = {"configurable": {"thread_id": run_id}}
    result = graph.invoke(initial_state(run_id=run_id, employer_key=key, employer_name=name,
                                        funding_usd=funding, pay_lines=lines), config)
    return graph, config, result


def _approve(services, graph, config):
    state = graph.get_state(config).values
    payload = graph.get_state(config).interrupts[0].value
    graph.invoke(Command(resume=demo_resume(services, state, payload)), config)
    return graph.get_state(config).values


# --------------------------------------------------------------------- 1. partial payout
def test_plan_partial_payout_is_smallest_first_and_keeps_reserve():
    lines = [ContractorPayroll("Big", Decimal("3400"), "EUR"), ContractorPayroll("Small", Decimal("2600"), "EUR")]
    plan = plan_partial_payout(lines, RATES, {"USD": Decimal("5000")})
    assert plan is not None
    assert plan.paid == (1,) and plan.held == (0,)
    assert plan.assessment.can_run
    # payroll + 149 run fee + 1,000 reserve fits inside the wallet
    assert plan.assessment.required_usd + plan.assessment.reserve_floor_usd <= Decimal("5000")
    assert plan.full.shortfall_usd > 0


def test_plan_partial_payout_none_when_smallest_line_does_not_fit_or_all_fit():
    assert plan_partial_payout([ContractorPayroll("X", Decimal("6000"), "EUR")], RATES,
                               {"USD": Decimal("5000")}) is None  # Borealis
    assert plan_partial_payout([ContractorPayroll("X", Decimal("100"), "EUR")], RATES,
                               {"USD": Decimal("5000")}) is None  # all affordable: nothing to split


def test_partial_payout_graph_pays_smallest_holds_rest_and_signs_exact_lines():
    services = build_demo_services()
    graph, config, result = _run_to_interrupt(services, funding="5000", lines=ACME_LINES)
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "approval" and payload["partial"] is True
    assert [l["name"] for l in payload["pay_lines"]] == ["Tomas Rivera"]
    assert [l["name"] for l in payload["held_lines"]] == ["Lena Fischer"]
    assert payload["assessment"]["full_shortfall_usd"] != "0.00"
    # the TAP-signed body binds exactly the lines that will be paid
    state = graph.get_state(config).values
    assert [l["name"] for l in state["pay_lines"]] == ["Tomas Rivera"]

    final = _approve(services, graph, config)
    assert final["status"] == "PARTIALLY_PAID"
    assert final["completed_payouts"] == [0]
    assert [l["name"] for l in final["held_lines"]] == ["Lena Fischer"]
    assert final["fee_charged_usd"] == "149.00"
    assert Decimal(final["balances"]["USD"]) >= Decimal("1000")  # reserve floor intact
    assert any(e["agent"] == "escalate" and "HELD" in e["message"] for e in final["events"])
    assert final["metal_receipt"] is not None
    assert services.ledger.is_balanced()


def test_partial_approval_cannot_be_signed_over_the_full_batch():
    services = build_demo_services()
    graph, config, _ = _run_to_interrupt(services, funding="5000", lines=ACME_LINES)
    state = dict(graph.get_state(config).values)
    forged = services.tap.sign_approval(
        run_id=state["run_id"], employer_key=state["employer_key"], decision="approve",
        assessment=state["assessment"], pay_lines=state["pay_lines"] + state["held_lines"],
        decided_by="x@example.com")
    graph.invoke(Command(resume={"decision": "approve", "decided_by": "x@example.com",
                                 "tap": forged.as_dict()}), config)
    final = graph.get_state(config).values
    assert final["status"] == "APPROVAL_REJECTED"
    assert final["completed_payouts"] == []


def test_borealis_single_large_line_is_still_fully_held():
    services = build_demo_services()
    spec = EMPLOYERS["borealis"]
    graph, config, result = _run_to_interrupt(services, funding=spec["funding_usd"], lines=spec["pay_lines"],
                                              key="borealis", name=spec["name"])
    assert result["__interrupt__"][0].value["kind"] == "escalation"
    assert graph.get_state(config).values["status"] == "HELD_SHORTFALL"
    assert not graph.get_state(config).values.get("held_lines")


def test_partial_payout_through_api_persists_paid_and_held_lines(demo_client):
    emp = {e["name"]: e for e in demo_client.get("/employers").json()}["Acme Manufacturing GmbH"]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    lines = [{"contractor_id": c["id"]} for c in detail["contractors"]]
    for _ in range(2):  # draw the wallet down until only one line is affordable
        run = demo_client.post("/runs", json={"employer_id": emp["id"], "lines": lines}).json()
        assert demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()[
            "status"] == "SETTLED"
    run = demo_client.post("/runs", json={"employer_id": emp["id"], "lines": lines}).json()
    assert run["status"] == "RESERVED"
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID"
    assert sorted(p["status"] for p in done["pay_lines"]) == ["HELD", "PAID"]
    assert Decimal(done["shortfall_usd"]) > 0


# --------------------------------------------------------------------- 2. post-payout failures
def _approved_run_with(services_mutator):
    services = build_demo_services()
    graph, config, _ = _run_to_interrupt(services, funding="20000", lines=ACME_LINES)
    services_mutator(services)
    return services, _approve(services, graph, config)


def test_fee_failure_is_recorded_not_raised():
    def boom(services):
        def raiser(**kw):
            raise RuntimeError("charge endpoint down")
        services.airwallex.create_charge = raiser

    _, final = _approved_run_with(boom)
    assert final["status"] == "SENT_FEE_FAILED"
    assert final["completed_payouts"] == [0, 1]  # contractors really were paid
    assert any(e["level"] == "error" and e["agent"] == "fee" for e in final["events"])


def test_reconcile_failure_is_recorded_not_raised():
    def boom(services):
        def raiser(*a, **k):
            raise RuntimeError("ledger offline")
        services.ledger.employer_cash = raiser

    _, final = _approved_run_with(boom)
    assert final["status"] == "SENT_UNRECONCILED"
    assert any(e["agent"] == "reconcile" and e["level"] == "error" for e in final["events"])


def test_attest_failure_does_not_mark_money_movement_failed():
    def boom(services):
        def raiser(payload):
            raise RuntimeError("metal rpc down")
        services.chain.attest = raiser

    _, final = _approved_run_with(boom)
    assert final["status"] == "ATTEST_PENDING"
    assert final["completed_payouts"] == [0, 1] and final["fee_charged_usd"] == "149.00"
    assert final["metal_receipt"] is None


@pytest.mark.parametrize("target,status", [("create_charge", "SENT_FEE_FAILED")])
def test_runtime_persists_post_payout_failure_via_api(demo_client, monkeypatch, target, status):
    runtime = demo_client.app.state.runtime

    def raiser(**kw):
        raise RuntimeError("charge endpoint down")

    monkeypatch.setattr(runtime.recorder, target, raiser)
    emp = {e["name"]: e for e in demo_client.get("/employers").json()}["Acme Manufacturing GmbH"]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    r = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == status
    assert all(p["status"] == "PAID" for p in body["pay_lines"])  # DB reflects the money that moved
    assert "Run fee failed" in (body["error"] or "")


def test_runtime_persists_state_even_when_graph_invoke_raises(demo_client, monkeypatch):
    runtime = demo_client.app.state.runtime
    emp = {e["name"]: e for e in demo_client.get("/employers").json()}["Acme Manufacturing GmbH"]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    real_invoke = runtime.graph.invoke

    def invoke_then_raise(payload, config=None, **kw):
        real_invoke(payload, config, **kw)  # money moves and the checkpoint is written...
        raise RuntimeError("socket closed")  # ...then the call blows up

    monkeypatch.setattr(runtime.graph, "invoke", invoke_then_raise)
    from fastapi.testclient import TestClient

    quiet = TestClient(demo_client.app, raise_server_exceptions=False)
    quiet.cookies.update(demo_client.cookies)
    r = quiet.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 500
    monkeypatch.undo()
    after = demo_client.get(f"/runs/{run['id']}").json()
    assert after["status"] == "SETTLED"  # not stuck at RESERVED
    assert all(p["status"] == "PAID" for p in after["pay_lines"])


# --------------------------------------------------------------------- 3. execute re-check + one open run
def test_execute_rechecks_affordability_and_moves_no_money_when_wallet_shrank():
    services = build_demo_services()
    graph, config, _ = _run_to_interrupt(services, funding="20000", lines=ACME_LINES)
    state = graph.get_state(config).values
    acct = state["account_id"]
    sim = services.airwallex
    tx_before = len(services.ledger.transactions)
    sim.get_balances = lambda account_id=None: {"USD": Decimal("1500")} if account_id == acct else {}
    final = _approve(services, graph, config)
    assert final["status"] == "HELD_SHORTFALL"
    assert "no longer covers" in final["error"]
    assert final["completed_payouts"] == [] and final["fx_done"] == []
    assert len(services.ledger.transactions) == tx_before  # nothing moved


def test_second_run_for_same_employer_is_rejected_while_first_is_open(demo_client):
    emp = {e["name"]: e for e in demo_client.get("/employers").json()}["Acme Manufacturing GmbH"]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    body = {"employer_id": emp["id"], "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}
    first = demo_client.post("/runs", json=body)
    assert first.status_code == 201 and first.json()["status"] == "RESERVED"
    second = demo_client.post("/runs", json=body)
    assert second.status_code == 409
    assert "open run" in second.json()["detail"]
    # another employer is unaffected
    cobalt = {e["name"]: e for e in demo_client.get("/employers").json()}["Cobalt Retail LLC"]
    cdetail = demo_client.get(f"/employers/{cobalt['id']}").json()
    other = demo_client.post("/runs", json={"employer_id": cobalt["id"],
                                            "lines": [{"contractor_id": c["id"]} for c in cdetail["contractors"]]})
    assert other.status_code == 201
    # once the first is decided, a new run may start
    assert demo_client.post(f"/runs/{first.json()['id']}/decision", json={"decision": "hold"}).status_code == 200
    assert demo_client.post("/runs", json=body).status_code == 201
