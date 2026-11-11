"""Pay held lines: a continuation run pays only a short run's HELD lines, never anyone twice."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from langgraph.types import Command
from sqlalchemy import select

from app.agents.demo import build_demo_services
from app.airwallex.simulator import request_id_for
from app.chat import tools as chat_tools
from app.db.models import Contractor, Membership, Organization, PayrollRun, User
from app.policy import ContractorPayroll, assess_payroll, plan_partial_payout

RATES = build_demo_services().usd_rates


# ------------------------------------------------------------------ helpers
def _emp(client, name):
    return {e["name"]: e for e in client.get("/employers").json()}[name]


def _delta_partial(client):
    """Delta Studios (USD 5,000): approve the run -> PARTIALLY_PAID, Pieter Jansen HELD."""
    emp = _emp(client, "Delta Studios BV")
    detail = client.get(f"/employers/{emp['id']}").json()
    run = client.post("/runs", json={"employer_id": emp["id"],
                                     "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    done = client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID", done
    return emp, detail, done


def _fund(client, emp, amount="4000"):
    r = client.post(f"/employers/{emp['id']}/fund", json={"amount": amount, "reference": uuid.uuid4().hex})
    assert r.status_code == 200, r.text


def _transfers(client):
    log = client.app.state.runtime.recorder.log
    return {c.request_id for c in log if c.operation == "create_transfer"}


def _pay_held(client, run_id):
    return client.post(f"/runs/{run_id}/pay-held")


def _login_viewer(client, owner_login, email="viewer.held@example.com"):
    inv = client.post("/org/invitations", json={"email": email, "role": "viewer"}).json()
    client.post(f"/invitations/{inv['invite_path'].split('/')[-1]}/accept",
                json={"full_name": "Viewer Held", "password": "ViewerPass2026"})
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": email, "password": "ViewerPass2026"})
    return owner_login


# ------------------------------------------------------------------ policy
def test_assess_payroll_waive_run_fee_drops_only_the_run_fee():
    lines = [ContractorPayroll("A", Decimal("1000"), "USD", "LOCAL")]
    wallet = {"USD": Decimal("2100")}  # 1000 + 1000 reserve + 100 spare: enough only without the 149 fee
    normal = assess_payroll(lines, RATES, wallet)
    waived = assess_payroll(lines, RATES, wallet, waive_run_fee=True)
    assert normal.platform_fee_usd == Decimal("149.00") and not normal.can_run
    assert normal.shortfall_usd == Decimal("49.00") and normal.run_fee_waived is False
    assert waived.platform_fee_usd == Decimal("0.00") and waived.can_run and waived.run_fee_waived is True
    assert waived.required_usd == Decimal("1000.00")
    assert waived.reserve_floor_usd == Decimal("1000.00")  # the reserve floor is never waived


def test_plan_partial_payout_exact_budget_with_and_without_run_fee():
    lines = [ContractorPayroll("Small", Decimal("1000"), "USD", "LOCAL"),
             ContractorPayroll("Large", Decimal("1500"), "USD", "LOCAL")]
    # 3,500 covers both lines + the reserve only when the run fee is waived.
    assert plan_partial_payout(lines, RATES, {"USD": Decimal("3500")}, waive_run_fee=True) is None
    normal = plan_partial_payout(lines, RATES, {"USD": Decimal("3500")})
    assert normal is not None and normal.paid == (0,) and normal.held == (1,)
    assert normal.assessment.platform_fee_usd == Decimal("149.00") and normal.full.shortfall_usd == Decimal("149.00")
    waived = plan_partial_payout(lines, RATES, {"USD": Decimal("2400")}, waive_run_fee=True)
    assert waived is not None and waived.paid == (0,) and waived.held == (1,)
    assert waived.assessment.platform_fee_usd == Decimal("0.00") and waived.assessment.run_fee_waived
    assert waived.full.shortfall_usd == Decimal("1100.00")
    with_fee = plan_partial_payout(lines, RATES, {"USD": Decimal("2400")})
    assert with_fee.assessment.platform_fee_usd == Decimal("149.00") and with_fee.full.shortfall_usd == Decimal("1249.00")
    # waiving the fee lets a wallet that is 100 short of (line + reserve + fee) pay that line
    assert plan_partial_payout(lines, RATES, {"USD": Decimal("2100")}, waive_run_fee=True).paid == (0,)
    assert plan_partial_payout(lines, RATES, {"USD": Decimal("2100")}) is None


# ------------------------------------------------------------------ happy path
def test_delta_pay_held_end_to_end_no_double_payment_and_no_second_run_fee(demo_client):
    emp, _detail, parent = _delta_partial(demo_client)
    assert parent["held_line_count"] == 1 and parent["can_pay_held"] is True
    assert parent["continuation_run_id"] is None and parent["parent_run_id"] is None
    parent_paid_ids = _transfers(demo_client)
    assert len(parent_paid_ids) == 1
    fees_before = demo_client.get("/overview").json()["platform_fees"]
    assert fees_before == {"USD": "149.00"}
    _fund(demo_client, emp)

    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 201, r.text
    cont = r.json()
    assert cont["status"] == "RESERVED" and cont["source"] == "continuation"
    assert cont["parent_run_id"] == parent["id"] and cont["id"] != parent["id"]
    assert [l["beneficiary_name"] for l in cont["pay_lines"]] == ["Pieter Jansen"]
    assert cont["pay_lines"][0]["amount"] == "3600.00"
    a = cont["assessment"]
    assert a["platform_fee_usd"] == "0.00" and a["run_fee_waived"] is True and a["can_run"] is True
    assert cont["pending"]["continuation"] is True and cont["pending"]["run_fee_waived"] is True
    assert cont["pending"]["parent_run_id"] == parent["id"]
    assert cont["explanation"] is not None
    assert cont["held_line_count"] == 0 and cont["can_pay_held"] is False
    # nothing moved yet
    assert _transfers(demo_client) == parent_paid_ids

    done = demo_client.post(f"/runs/{cont['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED", done
    assert done["fee_charged_usd"] == "0.00"
    assert any("already collected on the original payroll" in e["message"] for e in done["events"]
               if e["agent"] == "fee")
    # exactly one NEW transfer, request_id derived from the NEW run id; nobody paid twice
    new_ids = _transfers(demo_client) - parent_paid_ids
    assert new_ids == {request_id_for(cont["id"], "pay-0")}
    assert len(_transfers(demo_client)) == 2
    assert done["pay_lines"][0]["status"] == "PAID"
    # no second USD 149 run fee
    assert demo_client.get("/overview").json()["platform_fees"] == {"USD": "149.00"}

    after = demo_client.get(f"/runs/{parent['id']}").json()
    assert after["status"] == "PARTIALLY_PAID"  # the original run is never rewritten
    assert after["continuation_run_id"] == cont["id"] and after["can_pay_held"] is False
    assert {p["beneficiary_name"]: p["status"] for p in after["pay_lines"]} == {
        "Noor van Dijk": "PAID", "Pieter Jansen": "HELD"}
    # the approval of the continuation still verifies (bound to its own run)
    assert demo_client.post("/tap/verify", json={"run_id": cont["id"]}).json()["verified"] is True
    # list view exposes the link
    listed = {r["id"]: r for r in demo_client.get("/runs").json()}
    assert listed[cont["id"]]["parent_run_id"] == parent["id"] and listed[cont["id"]]["source"] == "continuation"
    # the books still balance and the wallet matches the ledger
    assert any("Reconciled" in e["message"] for e in done["events"])
    # settled: a second click is refused
    assert _pay_held(demo_client, parent["id"]).status_code == 409


def test_second_click_while_the_continuation_is_open_is_409(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    first = _pay_held(demo_client, parent["id"])
    assert first.status_code == 201
    again = _pay_held(demo_client, parent["id"])
    assert again.status_code == 409 and "already being paid" in again.json()["detail"]
    assert len({r["id"] for r in demo_client.get("/runs").json() if r["source"] == "continuation"}) == 1


def test_held_by_human_continuation_does_not_block_another_attempt(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    first = _pay_held(demo_client, parent["id"]).json()
    held = demo_client.post(f"/runs/{first['id']}/decision", json={"decision": "hold"}).json()
    assert held["status"] == "HELD_BY_HUMAN"
    assert demo_client.get(f"/runs/{parent['id']}").json()["can_pay_held"] is True
    second = _pay_held(demo_client, parent["id"])
    assert second.status_code == 201 and second.json()["id"] != first["id"]
    assert demo_client.get(f"/runs/{parent['id']}").json()["continuation_run_id"] == second.json()["id"]
    assert demo_client.post(f"/runs/{second.json()['id']}/decision", json={"decision": "approve"}).json()[
        "status"] == "SETTLED"
    assert len(_transfers(demo_client)) == 2  # Noor once, Pieter once


def test_pay_held_while_still_short_ends_held_with_exact_numbers_and_can_be_retried(demo_client):
    emp, _d, parent = _delta_partial(demo_client)  # wallet ~ USD 2,4xx: not enough for Pieter + reserve
    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 201, r.text
    cont = r.json()
    assert cont["status"] == "HELD_SHORTFALL" and cont["pending"]["kind"] == "escalation"
    assert Decimal(cont["shortfall_usd"]) > 0
    assert cont["assessment"]["platform_fee_usd"] == "0.00"  # waived: the shortfall excludes the run fee
    assert len(_transfers(demo_client)) == 1  # nothing moved
    parent_now = demo_client.get(f"/runs/{parent['id']}").json()
    assert parent_now["status"] == "PARTIALLY_PAID"
    # a continuation that ended HELD_SHORTFALL does not block another attempt
    assert parent_now["can_pay_held"] is True
    _fund(demo_client, emp)
    ok = _pay_held(demo_client, parent["id"])
    assert ok.status_code == 201 and ok.json()["status"] == "RESERVED"


def test_held_shortfall_parent_with_nothing_paid_charges_the_run_fee_normally(demo_client):
    emp = _emp(demo_client, "Borealis Labs AB")  # one EUR 6,000 line against USD 5,000: nothing fits
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    assert run["status"] == "HELD_SHORTFALL" and run["pending"]["kind"] == "escalation"
    parent = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "hold"}).json()
    assert parent["status"] == "HELD_SHORTFALL" and parent["held_line_count"] == 1
    assert parent["can_pay_held"] is True
    _fund(demo_client, emp, "5000")
    cont = _pay_held(demo_client, parent["id"]).json()
    assert cont["status"] == "RESERVED" and cont["source"] == "continuation"
    assert cont["assessment"]["platform_fee_usd"] == "149.00" and cont["assessment"]["run_fee_waived"] is False
    done = demo_client.post(f"/runs/{cont['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED" and done["fee_charged_usd"] == "149.00"
    assert demo_client.get("/overview").json()["platform_fees"] == {"USD": "149.00"}
    assert len(_transfers(demo_client)) == 1


def test_chain_a_partial_continuation_can_be_continued_again(demo_client):
    emp = _emp(demo_client, "Delta Studios BV")
    for name, amt in (("Quinn Maas", "3700"),):
        r = demo_client.post(f"/employers/{emp['id']}/contractors", json={
            "full_name": name, "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
            "iban": "NL91ABNA0417164300", "swift_code": "ABNANL2AXXX", "default_amount": amt})
        assert r.status_code == 201, r.text
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    a = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert a["status"] == "PARTIALLY_PAID" and a["held_line_count"] == 2, a
    assert a["fee_charged_usd"] == "149.00"
    _fund(demo_client, emp, "4500")
    b = _pay_held(demo_client, a["id"]).json()
    assert b["parent_run_id"] == a["id"] and b["assessment"]["platform_fee_usd"] == "0.00"
    b_done = demo_client.post(f"/runs/{b['id']}/decision", json={"decision": "approve"}).json()
    assert b_done["status"] == "PARTIALLY_PAID", b_done  # one more held
    assert b_done["held_line_count"] == 1 and b_done["can_pay_held"] is True
    assert b_done["fee_charged_usd"] == "0.00"
    _fund(demo_client, emp, "5000")
    c = _pay_held(demo_client, b["id"]).json()
    assert c["parent_run_id"] == b["id"]
    assert c["assessment"]["run_fee_waived"] is True  # fee collected two levels up the chain
    c_done = demo_client.post(f"/runs/{c['id']}/decision", json={"decision": "approve"}).json()
    assert c_done["status"] == "SETTLED" and c_done["fee_charged_usd"] == "0.00"
    # three contractors, three transfers, one run fee
    assert len(_transfers(demo_client)) == 3
    assert demo_client.get("/overview").json()["platform_fees"] == {"USD": "149.00"}
    # A cannot be continued again (B moved money), nor B (C moved money)
    assert _pay_held(demo_client, a["id"]).status_code == 409
    assert _pay_held(demo_client, b["id"]).status_code == 409
    assert demo_client.get(f"/runs/{a['id']}").json()["continuation_run_id"] == b["id"]


# ------------------------------------------------------------------ eligibility
def test_settled_and_unpaid_runs_have_nothing_to_continue(demo_client):
    emp = _emp(demo_client, "Acme Manufacturing GmbH")
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    assert run["status"] == "RESERVED" and run["can_pay_held"] is False
    r = _pay_held(demo_client, run["id"])
    assert r.status_code == 409 and "RESERVED" in r.json()["detail"]
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED" and done["held_line_count"] == 0 and done["can_pay_held"] is False
    assert _pay_held(demo_client, run["id"]).status_code == 409


def test_unknown_run_is_404(demo_client):
    assert _pay_held(demo_client, uuid.uuid4()).status_code == 404


def test_only_one_open_run_per_employer_still_applies(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp, "6000")
    other = demo_client.post("/runs", json={"employer_id": emp["id"], "lines": [
        {"contractor_id": c["id"]} for c in demo_client.get(f"/employers/{emp['id']}").json()["contractors"][:1]]})
    assert other.status_code == 201 and other.json()["status"] == "RESERVED"
    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 409 and "already has an open run" in r.json()["detail"]


def test_a_later_payroll_that_already_paid_the_held_contractor_blocks_double_payment(demo_client):
    emp, detail, parent = _delta_partial(demo_client)
    _fund(demo_client, emp, "9000")
    pieter = next(c for c in detail["contractors"] if c["full_name"] == "Pieter Jansen")
    later = demo_client.post("/runs", json={"employer_id": emp["id"], "lines": [{"contractor_id": pieter["id"]}]}).json()
    assert demo_client.post(f"/runs/{later['id']}/decision", json={"decision": "approve"}).json()["status"] == "SETTLED"
    assert demo_client.get(f"/runs/{parent['id']}").json()["can_pay_held"] is False
    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 409 and "Pieter Jansen" in r.json()["detail"]
    assert len(_transfers(demo_client)) == 2


def test_deactivated_held_contractor_is_422_and_named(demo_client):
    emp, detail, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    pieter = next(c for c in detail["contractors"] if c["full_name"] == "Pieter Jansen")
    assert demo_client.delete(f"/employers/{emp['id']}/contractors/{pieter['id']}").status_code == 204
    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 422 and "Pieter Jansen" in r.json()["detail"] and "deactivated" in r.json()["detail"]
    assert not [x for x in demo_client.get("/runs").json() if x["source"] == "continuation"]


def test_deleted_held_contractor_is_422_and_named(demo_client):
    emp, detail, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        s.delete(s.scalar(select(Contractor).where(Contractor.full_name == "Pieter Jansen")))
        s.commit()  # pay_lines.contractor_id -> SET NULL
    r = _pay_held(demo_client, parent["id"])
    assert r.status_code == 422 and "Pieter Jansen" in r.json()["detail"]


def test_corrected_bank_details_are_used_for_the_continuation(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        c = s.scalar(select(Contractor).where(Contractor.full_name == "Pieter Jansen"))
        c.iban = "NL86INGB0002445588"
        s.commit()
    cont = _pay_held(demo_client, parent["id"]).json()
    state = demo_client.app.state.runtime.graph.get_state({"configurable": {"thread_id": cont["id"]}}).values
    assert [l["iban"] for l in state["pay_lines"]] == ["NL86INGB0002445588"]
    assert state["pay_lines"][0]["amount"] == "3600.00"  # the held amount, not a fresh default


# ------------------------------------------------------------------ roles and tenancy
def test_viewer_cannot_start_pay_held(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    _login_viewer(demo_client, None)
    assert _pay_held(demo_client, parent["id"]).status_code == 403
    # a viewer still sees the run and the flag (role-independent)
    got = demo_client.get(f"/runs/{parent['id']}").json()
    assert got["can_pay_held"] is True


def test_approver_cannot_start_pay_held(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    inv = demo_client.post("/org/invitations", json={"email": "appr.held@example.com", "role": "approver"}).json()
    demo_client.post(f"/invitations/{inv['invite_path'].split('/')[-1]}/accept",
                     json={"full_name": "Appr Held", "password": "ApproverPass2026"})
    assert _pay_held(demo_client, parent["id"]).status_code == 403


def test_other_organization_cannot_see_or_act_on_the_parent(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    demo_client.post("/auth/logout")
    signup = {"organization": {"name": "Rival Payroll Org", "country": "US", "base_currency": "USD"},
              "admin": {"full_name": "Rival Owner", "email": "rival.held@example.com", "password": "RivalPass2026"}}
    assert demo_client.post("/auth/signup", json=signup).status_code == 201
    assert demo_client.get(f"/runs/{parent['id']}").status_code == 404
    assert _pay_held(demo_client, parent["id"]).status_code == 404
    assert len(_transfers(demo_client)) == 1


# ------------------------------------------------------------------ TAP approval is still required
def _continuation_with_pause(demo_client):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    return parent, _pay_held(demo_client, parent["id"]).json()


def test_unsigned_approval_of_a_continuation_is_rejected_and_pays_nothing(demo_client):
    _parent, cont = _continuation_with_pause(demo_client)
    runtime = demo_client.app.state.runtime
    config = {"configurable": {"thread_id": cont["id"]}}
    runtime.graph.invoke(Command(resume={"decision": "approve", "decided_by": "mallory@example.com"}), config)
    values = runtime.graph.get_state(config).values
    assert values["status"] == "APPROVAL_REJECTED" and "missing_signature" in values["error"]
    assert len(_transfers(demo_client)) == 1


def test_tampered_approval_of_a_continuation_is_rejected_and_pays_nothing(demo_client):
    _parent, cont = _continuation_with_pause(demo_client)
    runtime = demo_client.app.state.runtime
    config = {"configurable": {"thread_id": cont["id"]}}
    state = runtime.graph.get_state(config).values
    inflated = [dict(l, amount="9999.00") for l in state["pay_lines"]]
    envelope = runtime.signer.sign_approval(run_id=cont["id"], employer_key=state["employer_key"],
                                            decision="approve", assessment=state["assessment"],
                                            pay_lines=inflated, decided_by="mallory@example.com",
                                            held_lines=[])
    runtime.graph.invoke(Command(resume={"decision": "approve", "decided_by": "mallory@example.com",
                                         "tap": envelope.as_dict()}), config)
    assert runtime.graph.get_state(config).values["status"] == "APPROVAL_REJECTED"
    assert len(_transfers(demo_client)) == 1


def test_approval_signed_for_the_parent_run_cannot_approve_the_continuation(demo_client):
    parent, cont = _continuation_with_pause(demo_client)
    runtime = demo_client.app.state.runtime
    config = {"configurable": {"thread_id": cont["id"]}}
    state = runtime.graph.get_state(config).values
    envelope = runtime.signer.sign_approval(run_id=parent["id"], employer_key=state["employer_key"],
                                            decision="approve", assessment=state["assessment"],
                                            pay_lines=state["pay_lines"], decided_by="mallory@example.com",
                                            held_lines=[])
    runtime.graph.invoke(Command(resume={"decision": "approve", "decided_by": "mallory@example.com",
                                         "tap": envelope.as_dict()}), config)
    assert runtime.graph.get_state(config).values["status"] == "APPROVAL_REJECTED"


def test_relabelling_the_waived_fee_after_the_fact_breaks_the_signature(demo_client):
    _parent, cont = _continuation_with_pause(demo_client)
    runtime = demo_client.app.state.runtime
    config = {"configurable": {"thread_id": cont["id"]}}
    state = runtime.graph.get_state(config).values
    assert state["assessment"]["platform_fee_usd"] == "0.00"
    # The signed body binds required_usd (payroll + run fee): a signature over a USD 149 fee that this
    # waived run does not owe no longer matches what the run holds.
    forged = dict(state["assessment"], platform_fee_usd="149.00",
                  required_usd=str(Decimal(state["assessment"]["required_usd"]) + Decimal("149")))
    envelope = runtime.signer.sign_approval(run_id=cont["id"], employer_key=state["employer_key"],
                                            decision="approve", assessment=forged,
                                            pay_lines=state["pay_lines"], decided_by="x@example.com",
                                            held_lines=[])
    runtime.graph.invoke(Command(resume={"decision": "approve", "decided_by": "x@example.com",
                                         "tap": envelope.as_dict()}), config)
    assert runtime.graph.get_state(config).values["status"] == "APPROVAL_REJECTED"
    assert len(_transfers(demo_client)) == 1


# ------------------------------------------------------------------ migration
def test_parent_run_id_is_added_to_an_old_payroll_runs_table(tmp_path):
    import sqlite3

    from sqlalchemy import inspect

    from app.db import Database

    path = tmp_path / "old_runs.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE payroll_runs (
            id CHAR(32) NOT NULL PRIMARY KEY, organization_id CHAR(32) NOT NULL, employer_id CHAR(32) NOT NULL,
            status VARCHAR(20) NOT NULL, source VARCHAR(16) NOT NULL, fee_charged_usd NUMERIC(18, 2) NOT NULL, updated_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL
        );
        INSERT INTO payroll_runs VALUES ('a', 'o', 'e', 'SETTLED', 'manual', 149, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
    """)
    conn.commit()
    conn.close()
    db = Database.from_url(f"sqlite:///{path.as_posix()}")
    db.create_schema()
    insp = inspect(db.engine)
    assert "parent_run_id" in {c["name"] for c in insp.get_columns("payroll_runs")}
    assert "ix_payroll_runs_parent_run_id" in {i["name"] for i in insp.get_indexes("payroll_runs")}
    with db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT parent_run_id FROM payroll_runs").scalar() is None
    db.create_schema()  # idempotent


# ------------------------------------------------------------------ chat tool
@pytest.fixture
def ctx_factory(demo_client):
    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    sessions = []

    def make(role=None):
        s = db.sessionmaker()
        sessions.append(s)
        membership = s.scalars(select(Membership).where(Membership.role == "owner")).first()
        org = s.get(Organization, membership.organization_id)
        user = s.get(User, membership.user_id)
        return chat_tools.ChatContext(db=s, runtime=runtime, organization_id=org.id, organization_name=org.name,
                                      user_id=user.id, user_email=user.email, role=role or membership.role,
                                      demo_mode=True)
    yield make
    for s in sessions:
        s.close()


def test_chat_tool_starts_a_continuation_that_pauses_for_approval(demo_client, ctx_factory):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    ctx = ctx_factory()
    result = chat_tools.pay_held_lines(ctx, employer="Delta Studios BV")
    assert result.data["started"] is True and result.data["parent_run_id"] == parent["id"]
    assert result.approval is not None and "paused for a human decision" in result.text
    run = demo_client.get(f"/runs/{result.data['run_id']}").json()
    assert run["status"] == "RESERVED" and run["source"] == "continuation"
    assert len(_transfers(demo_client)) == 1  # chat moved nothing
    # a second ask is refused with the reason from the 409
    again = chat_tools.pay_held_lines(ctx_factory(), employer="Delta")
    assert again.data["started"] is False and "already being paid" in again.text


def test_chat_tool_role_gating_and_exact_employer(demo_client, ctx_factory):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    denied = chat_tools.pay_held_lines(ctx_factory(role="viewer"), employer="Delta Studios BV")
    assert "cannot" in denied.text and not denied.data
    assert chat_tools.pay_held_lines(ctx_factory(role="approver"), employer="Delta Studios BV").data == {}
    unknown = chat_tools.pay_held_lines(ctx_factory(), employer="Delt")
    assert "cannot find" in unknown.text
    nothing = chat_tools.pay_held_lines(ctx_factory(), employer="Acme Manufacturing GmbH")
    assert "no held lines" in nothing.text
    assert not [r for r in demo_client.get("/runs").json() if r["source"] == "continuation"]


def test_chat_tool_is_registered_and_fallback_routes_the_phrase(demo_client, ctx_factory):
    from app.chat import service as chat_service

    assert "pay_held_lines" in chat_tools.TOOL_FUNCTIONS and "pay_held_lines" in chat_tools.TOOL_LABELS
    tools, _ = chat_service.build_llm_tools(ctx_factory())
    schema = next(t for t in tools if t.name == "pay_held_lines").args_schema.model_json_schema()
    assert set(schema["properties"]) == {"employer", "run_id"}
    ctx = ctx_factory()
    steps, detail, _ = chat_service.fallback_plan(ctx, "pay the held lines for Delta Studios", {})
    assert steps[0][0] == "pay_held_lines" and steps[0][1]["employer"] == "Delta Studios BV"
    # "why is it held" is still an explanation, not an action
    steps, _, _ = chat_service.fallback_plan(ctx, "why is Delta Studios payroll held?", {})
    assert steps[0][0] == "explain_run"


def test_chat_tool_ignores_a_page_run_from_another_employer(demo_client, ctx_factory):
    emp, _d, parent = _delta_partial(demo_client)
    _fund(demo_client, emp)
    acme = _emp(demo_client, "Acme Manufacturing GmbH")
    detail = demo_client.get(f"/employers/{acme['id']}").json()
    acme_run = demo_client.post("/runs", json={"employer_id": acme["id"], "lines": [
        {"contractor_id": c["id"]} for c in detail["contractors"]]}).json()
    result = chat_tools.pay_held_lines(ctx_factory(), employer="Delta Studios BV", run_id=acme_run["id"])
    assert result.data["parent_run_id"] == parent["id"]  # Delta's own held run, not the Acme page run
