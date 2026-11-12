"""Per-employer settlement mode (ALL_OR_NOTHING | PARTIAL) and per-contractor payment priority."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text

from app.agents.demo import build_demo_services
from app.db import Database
from app.policy import ContractorPayroll, plan_partial_payout

RATES = build_demo_services().usd_rates


def _emp(client, name):
    return {e["name"]: e for e in client.get("/employers").json()}[name]


PEOPLE = [("Elena Brooks", "2000", 1), ("Marcus Reed", "3000", 2), ("Priya Nair", "1500", 3),
          ("Jonas Weber", "2000", 4), ("Aisha Bello", "1500", 5)]


def _dunmore(client, mode="PARTIAL"):
    """USD 5,000 wallet vs USD 10,000 over five prioritised USD LOCAL lines (no payout fee)."""
    emp = client.post("/employers", json={"name": "Dunmore Textiles", "country": "US", "settlement_mode": mode}).json()
    for i, (name, amount, prio) in enumerate(PEOPLE):
        r = client.post(f"/employers/{emp['id']}/contractors", json={
            "full_name": name, "country": "US", "currency": "USD", "payout_method": "LOCAL",
            "account_number": f"00023456789{i}", "routing_code": "021000021", "default_amount": amount,
            "priority": prio})
        assert r.status_code == 201, r.text
    assert client.post(f"/employers/{emp['id']}/fund", json={"amount": "5000"}).status_code == 200
    return client.get(f"/employers/{emp['id']}").json()


def _start(client, emp):
    detail = client.get(f"/employers/{emp['id']}").json()
    r = client.post("/runs", json={"employer_id": emp["id"],
                                   "lines": [{"contractor_id": c["id"]} for c in detail["contractors"]]})
    assert r.status_code in (200, 201), r.text
    return r.json()


# ------------------------------------------------------------------ policy
def test_priority_orders_the_walk_then_smallest_first_and_walk_continues():
    lines = [ContractorPayroll("Big", Decimal("3000"), "USD", "LOCAL", priority=1),
             ContractorPayroll("Small", Decimal("500"), "USD", "LOCAL", priority=2),
             ContractorPayroll("Mid", Decimal("900"), "USD", "LOCAL", priority=2)]
    # spendable = 5000 - 1000 reserve - 149 fee = 3851 -> Big fits (priority 1); Small fits; Mid does not (3500+900)
    plan = plan_partial_payout(lines, RATES, {"USD": Decimal("5000")})
    assert plan is not None and plan.paid == (0, 1) and plan.held == (2,)
    # a high-priority line that does not fit is held and a later smaller one is still paid (walk continues)
    plan = plan_partial_payout(lines, RATES, {"USD": Decimal("2000")})
    assert plan is not None and plan.paid == (1,) and plan.held == (0, 2)


def test_default_priority_keeps_smallest_first():
    lines = [ContractorPayroll("A", Decimal("2000"), "USD", "LOCAL"), ContractorPayroll("B", Decimal("1000"), "USD", "LOCAL")]
    plan = plan_partial_payout(lines, RATES, {"USD": Decimal("3000")})
    assert plan is not None and plan.paid == (1,) and plan.held == (0,)


# --------------------------------------------------------------------- API
def test_new_employers_default_to_all_or_nothing_and_it_is_editable(demo_client):
    r = demo_client.post("/employers", json={"name": "Mode Co", "country": "DE"})
    assert r.status_code == 201 and r.json()["settlement_mode"] == "ALL_OR_NOTHING"
    explicit = demo_client.post("/employers", json={"name": "Mode Co 2", "country": "DE", "settlement_mode": "PARTIAL"})
    assert explicit.json()["settlement_mode"] == "PARTIAL"
    p = demo_client.patch(f"/employers/{r.json()['id']}", json={"settlement_mode": "PARTIAL"})
    assert p.status_code == 200 and p.json()["settlement_mode"] == "PARTIAL"
    assert demo_client.patch(f"/employers/{r.json()['id']}", json={"settlement_mode": "SOMETIMES"}).status_code == 422


def test_viewer_cannot_change_settlement_mode(demo_client):
    emp = _dunmore(demo_client)
    inv = demo_client.post("/org/invitations", json={"email": "v.mode@example.com", "role": "viewer"}).json()
    demo_client.post(f"/invitations/{inv['invite_path'].split('/')[-1]}/accept",
                     json={"full_name": "Viewer", "password": "ViewerPass2026"})
    demo_client.post("/auth/logout")
    demo_client.post("/auth/login", json={"email": "v.mode@example.com", "password": "ViewerPass2026"})
    assert demo_client.patch(f"/employers/{emp['id']}", json={"settlement_mode": "ALL_OR_NOTHING"}).status_code == 403


def test_contractor_priority_is_editable(demo_client):
    emp = _dunmore(demo_client)
    c = demo_client.get(f"/employers/{emp['id']}").json()["contractors"][0]
    assert c["priority"] == 1
    r = demo_client.patch(f"/employers/{emp['id']}/contractors/{c['id']}", json={"priority": 9})
    assert r.status_code == 200 and r.json()["contractor"]["priority"] == 9
    assert demo_client.patch(f"/employers/{emp['id']}/contractors/{c['id']}", json={"priority": -1}).status_code == 422


def test_partial_mode_pays_by_priority_and_holds_the_rest(demo_client):
    emp = _dunmore(demo_client)
    assert emp["settlement_mode"] == "PARTIAL"
    run = _start(demo_client, emp)
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "PARTIALLY_PAID", done
    paid = {l["beneficiary_name"] for l in done["pay_lines"] if l["status"] == "PAID"}
    held = {l["beneficiary_name"] for l in done["pay_lines"] if l["status"] == "HELD"}
    assert paid == {"Elena Brooks", "Priya Nair"} and len(held) == 3  # Marcus 3,000 does not fit; walk continued


def test_all_or_nothing_pays_nobody_and_holds_the_run(demo_client):
    emp = _dunmore(demo_client)
    demo_client.patch(f"/employers/{emp['id']}", json={"settlement_mode": "ALL_OR_NOTHING"})
    run = _start(demo_client, emp)
    assert run["status"] == "HELD_SHORTFALL", run
    assert all(l["status"] == "HELD" for l in run["pay_lines"])
    assert Decimal(run["shortfall_usd"]) > 0
    transfers = [c for c in demo_client.app.state.runtime.recorder.log if c.operation == "create_transfer"]
    assert transfers == []  # nothing left the wallet
    assert "all or nothing" in " ".join(e["message"].lower() for e in run["events"])


def test_all_or_nothing_runs_normally_when_the_wallet_covers_everything(demo_client):
    emp = _emp(demo_client, "Acme Manufacturing GmbH")
    demo_client.patch(f"/employers/{emp['id']}", json={"settlement_mode": "ALL_OR_NOTHING"})
    run = _start(demo_client, emp)
    assert run["status"] == "RESERVED"


# --------------------------------------------------------------- migration
def test_migration_adds_columns_with_partial_default_for_existing_rows():
    from app.db.migrate import add_column_ddl
    from app.db.models import Contractor, Employer

    db = Database.from_url("sqlite://")
    mode = add_column_ddl(db.engine, "employers", Employer.__table__.c.settlement_mode)
    prio = add_column_ddl(db.engine, "contractors", Contractor.__table__.c.priority)
    assert "DEFAULT 'PARTIAL'" in mode and "NOT NULL" in mode  # existing employers keep today's behaviour
    assert "DEFAULT '100'" in prio
    with db.engine.begin() as conn:  # and the DDL really backfills existing rows
        conn.execute(text("CREATE TABLE employers (id CHAR(32) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO employers (id) VALUES ('a')"))
        conn.execute(text(mode))
        assert conn.execute(text("SELECT settlement_mode FROM employers")).scalar() == "PARTIAL"
