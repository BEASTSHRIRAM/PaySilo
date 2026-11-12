"""Travel & Accommodation (VIC spend) — QA + accounting verification.

Mirrors ``test_benefits.py`` style and pins money behaviour:

- seed invariants: Acme travel budget USD 2,000.00 with USD 340.00
  already spent (Tomas Rivera's COMPLETED Munich trip); Lena Fischer's
  Berlin trip is APPROVED with both components PENDING;
- category caps bind at DECISION time: a TRANSPORT total over its cap
  is refused with POLICY_CAP_EXCEEDED unless override_caps=true, which
  is recorded as caps_overridden;
- the USD 450.00 Flight is TX-limit blocked: its purchase records a
  FAILED TX_LIMIT_EXCEEDED row and the component stays PENDING, while
  the USD 180.00 Hotel on the same trip purchases fine;
- held payroll locks purchases; journals post only to travel_*
  accounts, balanced, never cash;
- serializer flags over_cap / cap / tx_limit_blocked and the
  deterministic policy_summary are correct.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

ACME = "Acme Manufacturing GmbH"
BOREALIS = "Borealis Labs AB"
COBALT = "Cobalt Retail LLC"
LENA = "Lena Fischer"
TOMAS = "Tomas Rivera"
INGRID = "Ingrid Solberg"

SEEDED_SPENT_ACME = "340.00"  # 60.00 + 180.00 + 100.00, Munich trip COMPLETED
BERLIN_SUMMARY = (
    "All components within policy caps. One component (Flight, USD 450.00) "
    "exceeds the active credential limit of USD 200 and cannot execute yet.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _employers(c: TestClient) -> dict[str, dict]:
    r = c.get("/employers")
    assert r.status_code == 200, r.text
    return {e["name"]: e for e in r.json()}


def _employer_detail(c: TestClient, name: str) -> dict:
    emp = _employers(c)[name]
    r = c.get(f"/employers/{emp['id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _contractor_id(c: TestClient, employer_name: str, person_name: str) -> str | None:
    for contractor in _employer_detail(c, employer_name).get("contractors", []):
        if contractor.get("full_name") == person_name:
            return str(contractor["id"])
    return None


def _budgets(c: TestClient) -> list[dict]:
    r = c.get("/travel/budgets")
    assert r.status_code == 200, r.text
    return r.json()


def _budget_for(c: TestClient, employer_name: str) -> dict:
    for row in _budgets(c):
        if row.get("employer_name") == employer_name:
            return row
    raise AssertionError(f"no travel budget for {employer_name!r}")


def _spent(c: TestClient, employer_name: str) -> str:
    return str(_budget_for(c, employer_name)["spent_this_period"])


def _trips(c: TestClient, employer_id: str | None = None) -> list[dict]:
    r = c.get("/travel/trips", params={"employer_id": employer_id} if employer_id else None)
    assert r.status_code == 200, r.text
    return r.json()


def _find_trip(c: TestClient, *, contractor_name: str, destination: str) -> dict:
    for trip in _trips(c):
        if trip["contractor_name"] == contractor_name and trip["destination"] == destination:
            return trip
    raise AssertionError(f"trip not found for {contractor_name!r} -> {destination!r}")


def _component(label: str, merchant: str, amount: str, kind: str) -> dict:
    return {"kind": kind, "label": label, "merchant": merchant, "amount": amount, "currency": "USD"}


def _create_trip(c: TestClient, employer_name: str, *, contractor_name: str, destination: str,
                 components: list[dict], purpose: str = "Client work") -> dict:
    employer_id = str(_employers(c)[employer_name]["id"])
    payload: dict = {
        "employer_id": employer_id, "destination": destination,
        "start_date": "2026-12-10", "end_date": "2026-12-12",
        "purpose": purpose, "components": components,
    }
    contractor_id = _contractor_id(c, employer_name, contractor_name)
    if contractor_id is not None:
        payload["contractor_id"] = contractor_id
    else:
        payload["contractor_name"] = contractor_name
    r = c.post("/travel/trips", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _decide(c: TestClient, trip_id: str, **body):
    payload = {"decision": "approve", **body}
    return c.post(f"/travel/trips/{trip_id}/decision", json=payload)


def _approve(c: TestClient, trip_id: str, **body) -> dict:
    r = _decide(c, trip_id, **body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED", r.json()
    return r.json()


def _purchase(c: TestClient, trip_id: str, component_id: str):
    return c.post(f"/travel/trips/{trip_id}/components/{component_id}/purchase", json={})


def _assert_refusal(response, code: str) -> dict:
    assert response.status_code == 409, response.text
    detail = response.json().get("detail")
    assert isinstance(detail, dict), response.text
    assert detail.get("code") == code, response.text
    assert detail.get("message"), response.text
    return detail


def _purchases(c: TestClient, trip_id: str | None = None) -> list[dict]:
    params = {"trip_id": trip_id} if trip_id else None
    r = c.get("/travel/purchases", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Seed state + serializer flags + policy_summary
# ---------------------------------------------------------------------------

def test_seed_state_and_flags(demo_client):
    budgets = {b["employer_name"]: b for b in _budgets(demo_client)}
    acme = budgets[ACME]
    assert str(acme["monthly_cap"]) == "2000.00", acme
    assert str(acme["spent_this_period"]) == SEEDED_SPENT_ACME, acme
    assert acme["enabled"] is True
    assert str(acme["cap_transport"]) == "750.00" and str(acme["cap_lodging"]) == "600.00", acme
    assert str(acme["cap_per_diem"]) == "300.00" and str(acme["cap_other"]) == "200.00", acme
    assert str(budgets[BOREALIS]["monthly_cap"]) == "500.00" and budgets[BOREALIS]["enabled"] is True
    assert str(budgets[COBALT]["monthly_cap"]) == "250.00" and budgets[COBALT]["enabled"] is False

    munich = _find_trip(demo_client, contractor_name=TOMAS, destination="Munich")
    assert munich["status"] == "COMPLETED", munich
    assert all(comp["status"] == "PURCHASED" for comp in munich["components"]), munich
    assert munich["policy_summary"] == "All components within policy caps.", munich
    succeeded = [p for p in _purchases(demo_client, str(munich["id"])) if p["status"] == "SUCCEEDED"]
    assert len(succeeded) == 3, succeeded
    assert all(p["idempotency_key"].startswith("trip:") for p in succeeded), succeeded

    berlin = _find_trip(demo_client, contractor_name=LENA, destination="Berlin")
    assert berlin["status"] == "APPROVED", berlin
    assert berlin["caps_overridden"] is False, berlin
    assert berlin["policy_summary"] == BERLIN_SUMMARY, berlin
    by_label = {comp["label"]: comp for comp in berlin["components"]}
    flight = by_label["Flight"]
    assert flight["status"] == "PENDING", flight
    assert flight["over_cap"] is False and flight["cap"] == "750.00", flight
    assert flight["tx_limit_blocked"] is True, flight
    hotel = by_label["Hotel night"]
    assert hotel["status"] == "PENDING", hotel
    assert hotel["over_cap"] is False and hotel["cap"] == "600.00", hotel
    assert hotel["tx_limit_blocked"] is False, hotel


# ---------------------------------------------------------------------------
# 2. Category cap refusal at decision + override
# ---------------------------------------------------------------------------

def test_category_cap_refusal_and_override(demo_client):
    trip = _create_trip(demo_client, ACME, contractor_name=LENA, destination="Frankfurt", components=[
        _component("Flight A", "Lufthansa", "400.00", "TRANSPORT"),
        _component("Flight B", "Lufthansa", "400.00", "TRANSPORT"),  # TRANSPORT total 800 > cap 750
        _component("Hotel night", "Frankfurt Hotel", "180.00", "LODGING"),
    ])
    detail = _assert_refusal(_decide(demo_client, str(trip["id"])), "POLICY_CAP_EXCEEDED")
    assert "Transport" in detail["message"], detail

    after = _find_trip(demo_client, contractor_name=LENA, destination="Frankfurt")
    assert after["status"] == "REQUESTED", after
    assert after["caps_overridden"] is False, after
    flight_a = next(comp for comp in after["components"] if comp["label"] == "Flight A")
    assert flight_a["over_cap"] is True and flight_a["cap"] == "750.00", flight_a
    assert "Transport exceeds its cap by USD 50.00" in after["policy_summary"], after

    approved = _approve(demo_client, str(trip["id"]), override_caps=True)
    assert approved["caps_overridden"] is True, approved
    assert "override approved" in approved["policy_summary"], approved


# ---------------------------------------------------------------------------
# 3. TX-limit component: FAILED row, stays PENDING; smaller component buys
# ---------------------------------------------------------------------------

def test_tx_limit_blocked_component(demo_client):
    berlin = _find_trip(demo_client, contractor_name=LENA, destination="Berlin")
    by_label = {comp["label"]: comp for comp in berlin["components"]}

    r = _purchase(demo_client, str(berlin["id"]), by_label["Flight"]["id"])
    assert r.status_code == 200, r.text
    purchase = r.json()
    assert purchase["status"] == "FAILED", purchase
    assert purchase["failure_code"] == "TX_LIMIT_EXCEEDED", purchase
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME  # blocked attempt moves no money

    after = _find_trip(demo_client, contractor_name=LENA, destination="Berlin")
    flight = next(comp for comp in after["components"] if comp["label"] == "Flight")
    assert flight["status"] == "PENDING", flight
    assert flight["failure_code"] == "TX_LIMIT_EXCEEDED", flight
    assert flight["tx_limit_blocked"] is True, flight

    hotel = next(comp for comp in after["components"] if comp["label"] == "Hotel night")
    r2 = _purchase(demo_client, str(after["id"]), hotel["id"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "SUCCEEDED", r2.json()
    assert _spent(demo_client, ACME) == "520.00"  # 340 seeded + 180 hotel

    final = _find_trip(demo_client, contractor_name=LENA, destination="Berlin")
    assert final["status"] == "PARTIALLY_PURCHASED", final


# ---------------------------------------------------------------------------
# 4. Held payroll locks travel purchases
# ---------------------------------------------------------------------------

def test_held_payroll_locks(demo_client):
    trip = _create_trip(demo_client, BOREALIS, contractor_name=INGRID, destination="Oslo",
                        components=[_component("Train", "SJ", "90.00", "TRANSPORT")])
    _approve(demo_client, str(trip["id"]))

    borealis = _employer_detail(demo_client, BOREALIS)
    run = demo_client.post(
        "/runs", json={"employer_id": borealis["id"],
                       "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    assert run["status"] == "HELD_SHORTFALL", run
    assert run["shortfall_usd"] == "2642.88", run

    component_id = trip["components"][0]["id"]
    _assert_refusal(_purchase(demo_client, str(trip["id"]), component_id), "PAYROLL_HELD")
    assert _spent(demo_client, BOREALIS) == "0.00"


# ---------------------------------------------------------------------------
# 5. Snapshot tamper invalidates the approval
# ---------------------------------------------------------------------------

def test_snapshot_tamper_invalidates(demo_client):
    trip = _create_trip(demo_client, ACME, contractor_name=TOMAS, destination="Hamburg",
                        components=[_component("Train", "DB", "90.00", "TRANSPORT")])
    _approve(demo_client, str(trip["id"]))
    component_id = trip["components"][0]["id"]

    from app.db.models import TripComponent

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        row = s.get(TripComponent, uuid.UUID(component_id))
        assert row is not None
        row.amount = Decimal("91.00")
        s.commit()

    _assert_refusal(_purchase(demo_client, str(trip["id"]), component_id), "SNAPSHOT_MISMATCH")
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME


# ---------------------------------------------------------------------------
# 6. Journal balanced, travel accounts only — never cash
# ---------------------------------------------------------------------------

def test_journal_travel_accounts_only(demo_client):
    amount = "70.00"
    trip = _create_trip(demo_client, ACME, contractor_name=TOMAS, destination="Hamburg",
                        components=[_component("Train", "DB", amount, "TRANSPORT")],
                        purpose="Journal probe")
    _approve(demo_client, str(trip["id"]))
    r = _purchase(demo_client, str(trip["id"]), trip["components"][0]["id"])
    assert r.status_code == 200, r.text
    purchase = r.json()
    assert purchase["status"] == "SUCCEEDED", purchase

    acme_id = str(_employers(demo_client)[ACME]["id"])
    journals = demo_client.get(f"/employers/{acme_id}/journals").json()
    txn = next((j for j in journals if str(j["id"]) == str(purchase["journal_txn_id"])), None)
    assert txn is not None, journals
    assert txn["balanced"] is True, txn
    assert any("travel_expense" in line["account"] and str(line.get("debit")) == amount
               for line in txn["lines"]), txn
    assert any("travel_budget" in line["account"] and str(line.get("credit")) == amount
               for line in txn["lines"]), txn
    assert all("cash" not in line["account"].split(":") for line in txn["lines"]), txn
    assert all("travel_" in line["account"] for line in txn["lines"]), txn
    total_debit = sum(Decimal(str(line.get("debit", "0"))) for line in txn["lines"])
    total_credit = sum(Decimal(str(line.get("credit", "0"))) for line in txn["lines"])
    assert total_debit == total_credit == Decimal(amount), txn


# ---------------------------------------------------------------------------
# 7. policy_summary service function (chat read path uses the same code)
# ---------------------------------------------------------------------------

def test_policy_summary_service(demo_client):
    from sqlalchemy import select

    from app.db.models import TravelBudget, TripRequest
    from app.services.travel import policy_summary

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        berlin = s.scalar(select(TripRequest).where(TripRequest.destination == "Berlin"))
        budget = s.scalar(select(TravelBudget).where(
            TravelBudget.employer_id == berlin.employer_id, TravelBudget.currency == "USD"))
        assert policy_summary(berlin, budget) == BERLIN_SUMMARY
        munich = s.scalar(select(TripRequest).where(TripRequest.destination == "Munich"))
        assert policy_summary(munich, budget) == "All components within policy caps."
