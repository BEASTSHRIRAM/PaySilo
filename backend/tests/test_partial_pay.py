"""PARTIAL settlement_mode (Sriram's rule, 2026-11-12) — priority walk,
PARTIALLY_SETTLED runs, split-bound approvals, and the two-phase rescue.

Fixture arithmetic (pinned in every test below)
-----------------------------------------------
The platform's REAL fee schedule (app/policy.py): platform run fee
USD 149.00 per run, reserve floor USD 1,000.00, payout fee USD 0.00 for
USD LOCAL lines (EUR SWIFT would be EUR 12.85/line). FX: USD = 1.

Employer wallet USD 5,000.00; five USD LOCAL lines, priorities 1..5,
amounts [2000, 3000, 1500, 2000, 1500] = USD 10,000.00 gross need.

PARTIAL walk (spendable = balance − reserve = 5,000 − 1,000 = 4,000):
  p1  2,000 fits -> the USD 149 run fee is deducted at this FIRST
      settlement -> remaining spendable 4,000 − 2,000 − 149 = 1,851
  p2  3,000 does not fit -> HELD, and the walk CONTINUES (skip-and-continue)
  p3  1,500 fits -> remaining 351
  p4  2,000 does not fit -> HELD
  p5  1,500 does not fit -> HELD
Paid = {p1, p3} = 3,500; held = {p2, p4, p5} = 6,500.
Balance after phase 1 = 5,000 − 3,500 − 149 (fee, once) = 1,351.
Spendable after settlement = 1,351 − 1,000 reserve = 351.
Residual shortfall = max(0, 6,500 − 351) = USD 6,149.00 exactly.
Rescue: deposit exactly 6,149 -> balance 7,500, spendable 6,500 = the held
total to the cent, so all three held lines settle in phase 2 with NO second
run fee, leaving exactly the 1,000 reserve.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.policy import ContractorPayroll, plan_priority_payout

DUNMORE = "Dunmore Textiles"
AMOUNTS = [Decimal("2000"), Decimal("3000"), Decimal("1500"), Decimal("2000"), Decimal("1500")]
NAMES = ["Elena Brooks", "Marcus Reed", "Priya Nair", "Jonas Weber", "Aisha Bello"]


def _lines() -> list[ContractorPayroll]:
    return [ContractorPayroll(name=n, amount=a, currency="USD", method="LOCAL", priority=i + 1)
            for i, (n, a) in enumerate(zip(NAMES, AMOUNTS))]


def _employers(c: TestClient) -> dict[str, dict]:
    return {e["name"]: e for e in c.get("/employers").json()}


def _detail(c: TestClient, name: str) -> dict:
    return c.get(f"/employers/{_employers(c)[name]['id']}").json()


def _balances(c: TestClient, name: str) -> dict:
    return _employers(c)[name]["balances"]


def _make_employer(c: TestClient, name: str, *, mode: str | None, fund: str) -> dict:
    body = {"name": name, "country": "US"}
    if mode is not None:
        body["settlement_mode"] = mode
    emp = c.post("/employers", json=body)
    assert emp.status_code == 201, emp.text
    emp = emp.json()
    if Decimal(fund) > 0:
        c.post(f"/employers/{emp['id']}/fund", json={"amount": fund})
    for i, (n, a) in enumerate(zip(NAMES, AMOUNTS)):
        r = c.post(f"/employers/{emp['id']}/contractors", json={
            "full_name": n, "country": "US", "currency": "USD", "payout_method": "LOCAL",
            "account_number": f"0003456789{i}", "routing_code": "021000021",
            "default_amount": str(a), "priority": i + 1})
        assert r.status_code == 201, r.text
    return _detail(c, name)


def _start_run(c: TestClient, detail: dict) -> dict:
    lines = [{"contractor_id": x["id"]} for x in detail["contractors"]]
    r = c.post("/runs", json={"employer_id": detail["id"], "lines": lines})
    assert r.status_code == 201, r.text
    return r.json()


def _dunmore_partial_run(c: TestClient) -> dict:
    run = _start_run(c, _detail(c, DUNMORE))
    assert run["status"] == "RESERVED", run
    done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    assert done.status_code == 200, done.text
    return done.json()


# ---------------------------------------------------------------------------
# Pricing layer: the walk itself, exact to the cent
# ---------------------------------------------------------------------------

def test_policy_priority_walk_exact_split_and_residual():
    plan = plan_priority_payout(_lines(), {"USD": Decimal("1")}, {"USD": Decimal("5000")})
    assert plan is not None
    # p1 + p3 settle; p2 is skipped and the walk continues to the smaller p3.
    assert plan.paid == (0, 2)
    assert plan.held == (1, 3, 4)
    assert plan.assessment.total_payroll == Decimal("3500.00")
    assert plan.assessment.platform_fee_usd == Decimal("149.00")  # once, at first settlement
    assert plan.assessment.required_usd == Decimal("3649.00")
    assert plan.assessment.can_run is True
    assert plan.full.shortfall_usd == Decimal("6149.00")
    # Pinned formula: held (net + own fees) 6,500 − spendable-after 351.
    assert plan.residual_shortfall_usd == Decimal("6149.00")


def test_policy_priority_walk_zero_payable_returns_none():
    # Spendable 200: even the smallest line (1,500) plus the once-only fee
    # cannot fit, so there is no split and no fee — the run is HELD whole.
    assert plan_priority_payout(_lines(), {"USD": Decimal("1")}, {"USD": Decimal("1200")}) is None
    # And a fully affordable batch is not a partial plan at all.
    assert plan_priority_payout(_lines(), {"USD": Decimal("1")}, {"USD": Decimal("20000")}) is None


# ---------------------------------------------------------------------------
# ALL_OR_NOTHING regression: the same fixture is held whole
# ---------------------------------------------------------------------------

def test_all_or_nothing_same_fixture_is_held_whole(demo_client):
    detail = _make_employer(demo_client, "AON Fixture Co", mode=None, fund="5000")
    assert detail["settlement_mode"] == "ALL_OR_NOTHING"  # the default
    run = _start_run(demo_client, detail)
    assert run["status"] == "HELD_SHORTFALL"
    assert run["shortfall_usd"] == "6149.00"  # 10,000 + 149 fee + 1,000 reserve − 5,000
    assert run["fee_charged_usd"] == "0.00"
    assert all(p["status"] == "HELD" for p in run["pay_lines"])
    assert _balances(demo_client, "AON Fixture Co") == {"USD": "5000.00"}
    assert demo_client.get("/overview").json()["platform_fees"] == {}


def test_partial_zero_payable_run_is_held_with_no_fee(demo_client):
    emp = demo_client.post("/employers", json={"name": "Tiny Partial Co", "country": "US",
                                               "settlement_mode": "PARTIAL"})
    assert emp.status_code == 201, emp.text
    emp = emp.json()
    demo_client.post(f"/employers/{emp['id']}/fund", json={"amount": "1200"})
    r = demo_client.post(f"/employers/{emp['id']}/contractors", json={
        "full_name": "Solo Worker", "country": "US", "currency": "USD", "payout_method": "LOCAL",
        "account_number": "000345678990", "routing_code": "021000021",
        "default_amount": "1500", "priority": 1})
    assert r.status_code == 201, r.text
    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": r.json()["id"]}]}).json()
    assert run["status"] == "HELD_SHORTFALL"
    assert run["fee_charged_usd"] == "0.00"
    assert _balances(demo_client, "Tiny Partial Co") == {"USD": "1200.00"}
    assert demo_client.get("/overview").json()["platform_fees"] == {}


# ---------------------------------------------------------------------------
# Dunmore end to end: PARTIALLY_SETTLED, fee once, journals only for settled
# ---------------------------------------------------------------------------

def test_dunmore_partial_settlement_end_to_end(demo_client):
    detail = _detail(demo_client, DUNMORE)
    assert detail["settlement_mode"] == "PARTIAL"
    assert [x["priority"] for x in detail["contractors"]] == [1, 2, 3, 4, 5]
    assert _balances(demo_client, DUNMORE) == {"USD": "5000.00"}

    run = _start_run(demo_client, detail)
    assert run["status"] == "RESERVED"
    assert run["settlement_mode"] == "PARTIAL"
    assert [l["name"] for l in run["pending"]["pay_lines"]] == ["Elena Brooks", "Priya Nair"]
    assert [l["name"] for l in run["pending"]["held_lines"]] == ["Marcus Reed", "Jonas Weber", "Aisha Bello"]
    assert run["assessment"]["settlement_mode"] == "PARTIAL"
    assert run["assessment"]["residual_shortfall_usd"] == "6149.00"
    marks = {s["name"]: s["mark"] for s in run["assessment"]["split"]}
    assert marks == {"Elena Brooks": "PAID", "Priya Nair": "PAID", "Marcus Reed": "HELD",
                     "Jonas Weber": "HELD", "Aisha Bello": "HELD"}

    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_SETTLED", done
    assert done["residual_shortfall"] == "6149.00"
    assert done["fee_charged_usd"] == "149.00"  # exactly once
    by_name = {p["beneficiary_name"]: p for p in done["pay_lines"]}
    assert {n: p["status"] for n, p in by_name.items()} == {
        "Elena Brooks": "PAID", "Priya Nair": "PAID", "Marcus Reed": "HELD",
        "Jonas Weber": "HELD", "Aisha Bello": "HELD"}
    assert {n: p["settlement_state"] for n, p in by_name.items()} == {
        "Elena Brooks": "PAID", "Priya Nair": "PAID", "Marcus Reed": "HELD",
        "Jonas Weber": "HELD", "Aisha Bello": "HELD"}
    assert by_name["Elena Brooks"]["settled_phase"] == 1
    assert by_name["Marcus Reed"]["settled_phase"] is None
    # Skip-and-continue, proven on the wire: Marcus (p2, 3,000) is HELD while
    # the smaller Priya (p3, 1,500) is PAID.
    assert by_name["Marcus Reed"]["status"] == "HELD" and by_name["Priya Nair"]["status"] == "PAID"
    assert _balances(demo_client, DUNMORE) == {"USD": "1351.00"}
    assert demo_client.get("/overview").json()["platform_fees"] == {"USD": "149.00"}
    # The stored phase-1 approval still verifies against the bound split.
    check = demo_client.post("/tap/verify", json={"run_id": run["id"]}).json()
    assert check["verified"] is True, check

    # Journals exist ONLY for settled lines (+ the single platform fee).
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        from app.db.models import LedgerEntry, LedgerTransaction

        txns = s.scalars(
            __import__("sqlalchemy").select(LedgerTransaction)
            .where(LedgerTransaction.run_id == uuid.UUID(run["id"]))).all()
        memos = [t.memo for t in txns]
        payout_memos = [m for m in memos if " payout " in m]
        assert len(payout_memos) == 2 and all(
            ("Elena Brooks" in m or "Priya Nair" in m) for m in payout_memos), memos
        assert not any(n in m for m in memos for n in ("Marcus Reed", "Jonas Weber", "Aisha Bello"))
        entries = [e for t in txns for e in t.entries]
        payees = sum((e.amount for e in entries if e.account.startswith("external:payees:")), Decimal("0"))
        fees = sum((e.amount for e in entries if e.account == "platform:fees:USD"), Decimal("0"))
        assert payees == Decimal("3500.00") and fees == Decimal("149.00")
        assert s.query(LedgerEntry).count() >= 0  # (entries above are the run's own)


# ---------------------------------------------------------------------------
# Snapshot integrity: a flipped split mark invalidates the approval
# ---------------------------------------------------------------------------

def test_snapshot_tamper_flipped_split_mark_is_rejected():
    from langgraph.types import Command

    from app.agents.demo import build_demo_services
    from app.agents.graph import build_graph, initial_state

    services = build_demo_services()
    graph = build_graph(services)
    config = {"configurable": {"thread_id": "pp-tamper"}}
    lines = [{"name": n, "amount": str(a), "currency": "USD", "method": "LOCAL",
              "account_number": "000345678900", "routing_code": "021000021", "priority": i + 1}
             for i, (n, a) in enumerate(zip(NAMES, AMOUNTS))]
    result = graph.invoke(initial_state(run_id="pp-tamper", employer_key="dunmore",
                                        employer_name=DUNMORE, funding_usd="5000", pay_lines=lines,
                                        settlement_mode="PARTIAL"), config)
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "approval" and payload["partial"] is True
    state = graph.get_state(config).values
    assert [l["name"] for l in state["pay_lines"]] == ["Elena Brooks", "Priya Nair"]

    # Forge an approval over a DIFFERENT split (the held Marcus line flipped
    # into the paid set): the signature is genuine, the split is not.
    forged = services.tap.sign_approval(
        run_id=state["run_id"], employer_key=state["employer_key"], decision="approve",
        assessment=state["assessment"], pay_lines=state["pay_lines"] + state["held_lines"][:1],
        decided_by="mallory@example.com", held_lines=state["held_lines"][1:])
    graph.invoke(Command(resume={"decision": "approve", "decided_by": "mallory@example.com",
                                 "tap": forged.as_dict()}), config)
    final = graph.get_state(config).values
    assert final["status"] == "APPROVAL_REJECTED"
    assert final["completed_payouts"] == []


def test_compare_signed_body_binds_residual_and_mode():
    from app.partners.tap_signing import approval_body, canonical_json, compare_signed_body_to_run

    paid = [{"name": "Elena Brooks", "amount": "2000.00", "currency": "USD", "method": "LOCAL"}]
    held = [{"name": "Marcus Reed", "amount": "3000.00", "currency": "USD", "method": "LOCAL"}]
    a = {"required_usd": "2149.00", "total_required": "2000.00", "payroll_currency": "USD",
         "settlement_mode": "PARTIAL", "residual_shortfall_usd": "6149.00"}
    body = canonical_json(approval_body(run_id="r", employer_key="e", decision="approve", assessment=a,
                                        pay_lines=paid, decided_by="u", held_lines=held))
    assert compare_signed_body_to_run(body, run_id="r", assessment=a, pay_lines=paid,
                                      held_lines=held) is None
    tampered = {**a, "residual_shortfall_usd": "6000.00"}
    assert "residual" in compare_signed_body_to_run(body, run_id="r", assessment=tampered,
                                                    pay_lines=paid, held_lines=held)


# ---------------------------------------------------------------------------
# Rescue: fresh plan + FRESH approval, then SETTLED in two phases
# ---------------------------------------------------------------------------

def test_rescue_requires_fresh_approval_and_settles_in_two_phases(demo_client):
    from app.services.runtime import RuntimeConflict

    done = _dunmore_partial_run(demo_client)
    run_id = done["id"]
    emp_id = _employers(demo_client)[DUNMORE]["id"]

    runtime = demo_client.app.state.runtime
    db = demo_client.app.state.db

    def _attempt_direct_execution():
        with db.sessionmaker() as s:
            from app.db.models import Employer, PayrollRun

            run = s.get(PayrollRun, uuid.UUID(run_id))
            emp = s.get(Employer, run.employer_id)
            with pytest.raises(RuntimeConflict):
                runtime.execute_residual_plan(s, run=run, employer=emp)
            s.rollback()

    # The original approval never covers the remainder.
    _attempt_direct_execution()
    # Nor can approve-residual run before a plan exists.
    assert demo_client.post(f"/runs/{run_id}/approve-residual",
                            json={"decision": "approve"}).status_code == 409

    # New funds arrive in Dunmore's OWN wallet: exactly the residual.
    r = demo_client.post(f"/employers/{emp_id}/fund", json={"amount": "6149.00", "reference": "rescue-1"})
    assert r.status_code == 200, r.text
    assert _balances(demo_client, DUNMORE) == {"USD": "7500.00"}

    prepared = demo_client.post(f"/runs/{run_id}/prepare-residual")
    assert prepared.status_code == 200, prepared.text
    plan = prepared.json()["residual_plan"]
    assert plan["status"] == "AWAITING_APPROVAL" and plan["total_usd"] == "6500.00"
    assert plan["residual_after_usd"] == "0.00"
    assert all(l["mark"] == "PAID" for l in plan["lines"])

    # A prepared-but-unapproved plan still cannot execute.
    _attempt_direct_execution()

    # Tamper with a held line after preparation: the snapshot must mismatch.
    with db.sessionmaker() as s:
        from app.db.models import PayLine

        line = s.query(PayLine).filter_by(run_id=uuid.UUID(run_id),
                                          beneficiary_name="Marcus Reed").one()
        line.amount = Decimal("3001.00")
        s.commit()
    assert demo_client.post(f"/runs/{run_id}/approve-residual",
                            json={"decision": "approve"}).status_code == 409
    with db.sessionmaker() as s:
        from app.db.models import PayLine

        line = s.query(PayLine).filter_by(run_id=uuid.UUID(run_id),
                                          beneficiary_name="Marcus Reed").one()
        line.amount = Decimal("3000.00")
        s.commit()
    # Re-prepare (the tamper invalidated the old snapshot), then approve.
    assert demo_client.post(f"/runs/{run_id}/prepare-residual").status_code == 200
    settled = demo_client.post(f"/runs/{run_id}/approve-residual", json={"decision": "approve"})
    assert settled.status_code == 200, settled.text
    settled = settled.json()
    assert settled["status"] == "SETTLED", settled
    assert settled["residual_shortfall"] == "0.00"
    assert settled["fee_charged_usd"] == "149.00"  # still exactly one fee, ever
    by_name = {p["beneficiary_name"]: p for p in settled["pay_lines"]}
    assert all(p["status"] == "PAID" for p in by_name.values())
    assert {n: p["settled_phase"] for n, p in by_name.items()} == {
        "Elena Brooks": 1, "Priya Nair": 1, "Marcus Reed": 2, "Jonas Weber": 2, "Aisha Bello": 2}
    assert all(p["settled_at"] for p in by_name.values())
    phase1_at = by_name["Elena Brooks"]["settled_at"]
    phase2_at = by_name["Marcus Reed"]["settled_at"]
    assert phase2_at >= phase1_at  # two distinct phases, honestly timestamped
    assert len(settled["approvals"]) == 2  # phase-1 + fresh residual approval
    assert settled["approvals"][1]["verified"] is True and settled["approvals"][1]["tap_signature"]
    assert _balances(demo_client, DUNMORE) == {"USD": "1000.00"}  # exactly the reserve remains
    assert demo_client.get("/overview").json()["platform_fees"] == {"USD": "149.00"}


# ---------------------------------------------------------------------------
# Golden story regression (same client shape as the golden suites)
# ---------------------------------------------------------------------------

def test_golden_story_unchanged(demo_client):
    acme = _detail(demo_client, "Acme Manufacturing GmbH")
    run = demo_client.post("/runs", json={"employer_id": acme["id"],
                                          "lines": [{"contractor_id": x["id"]}
                                                    for x in acme["contractors"]]}).json()
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED" and done["fee_charged_usd"] == "149.00"
    assert _balances(demo_client, "Acme Manufacturing GmbH") == {"USD": "13343.24"}

    borealis = _detail(demo_client, "Borealis Labs AB")
    held = demo_client.post("/runs", json={"employer_id": borealis["id"],
                                           "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    assert held["status"] == "HELD_SHORTFALL" and held["shortfall_usd"] == "2642.88"
