"""Onboarding Kit (VIC spend) — QA + accounting verification.

Mirrors ``test_benefits.py`` style and pins money behaviour:

- seed invariants: Acme onboarding budget USD 1,000.00 with USD 126.00
  already spent (Marta Kowalski's COMPLETED kit, journal-backed);
  Cobalt's budget is DISABLED at USD 250.00;
- snapshot tamper -> SNAPSHOT_MISMATCH; per-item idempotency (debited
  once); a TX-limit-blocked item leaves the kit PARTIALLY_PURCHASED
  while other items purchase; held payroll locks purchases
  (PAYROLL_HELD); budgets are per-employer isolated; journals post only
  to onboarding_* accounts, balanced, never cash; monthly cap refusal.
- template drafting (addendum): draft_kit_from_template records
  source=WORKFLOW with the exact draft_reason shape and is idempotent.
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
MARTA = "Marta Kowalski"

SEEDED_SPENT_ACME = "126.00"  # 40.00 + 36.00 + 50.00, Marta's COMPLETED kit


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
    r = c.get("/onboarding/budgets")
    assert r.status_code == 200, r.text
    return r.json()


def _budget_for(c: TestClient, employer_name: str) -> dict:
    for row in _budgets(c):
        if row.get("employer_name") == employer_name:
            return row
    raise AssertionError(f"no onboarding budget for {employer_name!r}")


def _spent(c: TestClient, employer_name: str) -> str:
    return str(_budget_for(c, employer_name)["spent_this_period"])


def _kits(c: TestClient, employer_id: str | None = None) -> list[dict]:
    r = c.get("/onboarding/kits", params={"employer_id": employer_id} if employer_id else None)
    assert r.status_code == 200, r.text
    return r.json()


def _find_kit(c: TestClient, *, contractor_name: str, status: str | None = None) -> dict:
    for kit in _kits(c):
        if kit["contractor_name"] != contractor_name:
            continue
        if status is not None and kit["status"] != status:
            continue
        return kit
    raise AssertionError(f"kit not found for {contractor_name!r} status={status!r}")


def _create_kit(c: TestClient, employer_name: str, *, contractor_name: str,
                items: list[dict], **extra) -> dict:
    employer_id = str(_employers(c)[employer_name]["id"])
    payload: dict = {"employer_id": employer_id, "items": items, **extra}
    contractor_id = _contractor_id(c, employer_name, contractor_name)
    if contractor_id is not None:
        payload["contractor_id"] = contractor_id
    else:
        payload["contractor_name"] = contractor_name
    r = c.post("/onboarding/kits", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _item(label: str, merchant: str, amount: str, kind: str = "CUSTOM") -> dict:
    return {"kind": kind, "label": label, "merchant": merchant, "amount": amount, "currency": "USD"}


def _approve(c: TestClient, kit_id: str) -> dict:
    r = c.post(f"/onboarding/kits/{kit_id}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED", r.json()
    return r.json()


def _purchase_item(c: TestClient, kit_id: str, item_id: str):
    return c.post(f"/onboarding/kits/{kit_id}/items/{item_id}/purchase", json={})


def _assert_refusal(response, code: str) -> dict:
    assert response.status_code == 409, response.text
    detail = response.json().get("detail")
    assert isinstance(detail, dict), response.text
    assert detail.get("code") == code, response.text
    assert detail.get("message"), response.text
    return detail


def _purchases(c: TestClient, kit_id: str | None = None) -> list[dict]:
    params = {"kit_id": kit_id} if kit_id else None
    r = c.get("/onboarding/purchases", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Seed state + provenance defaults
# ---------------------------------------------------------------------------

def test_seed_state(demo_client):
    budgets = {b["employer_name"]: b for b in _budgets(demo_client)}
    assert str(budgets[ACME]["monthly_cap"]) == "1000.00", budgets[ACME]
    assert str(budgets[ACME]["spent_this_period"]) == SEEDED_SPENT_ACME, budgets[ACME]
    assert budgets[ACME]["enabled"] is True
    assert str(budgets[BOREALIS]["monthly_cap"]) == "500.00" and budgets[BOREALIS]["enabled"] is True
    assert str(budgets[COBALT]["monthly_cap"]) == "250.00" and budgets[COBALT]["enabled"] is False

    marta = _find_kit(demo_client, contractor_name=MARTA, status="COMPLETED")
    assert marta["source"] == "MANUAL" and marta["draft_reason"] is None, marta
    assert len(marta["items"]) == 3, marta
    assert all(i["status"] == "PURCHASED" for i in marta["items"]), marta
    succeeded = [p for p in _purchases(demo_client, str(marta["id"])) if p["status"] == "SUCCEEDED"]
    assert len(succeeded) == 3, succeeded
    assert sum(Decimal(p["amount"]) for p in succeeded) == Decimal(SEEDED_SPENT_ACME), succeeded
    assert all(p["mode"] == "SIMULATED" for p in succeeded), succeeded
    assert all(p["idempotency_key"].startswith("kit:") for p in succeeded), succeeded

    draft = _find_kit(demo_client, contractor_name=LENA, status="DRAFT")
    assert all(i["status"] == "PENDING" for i in draft["items"]), draft

    status = demo_client.get("/onboarding/status")
    assert status.status_code == 200 and status.json()["mode"] == "SIMULATED", status.text


# ---------------------------------------------------------------------------
# 2. Snapshot tamper invalidates the approval
# ---------------------------------------------------------------------------

def test_snapshot_tamper_invalidates(demo_client):
    kit = _create_kit(demo_client, ACME, contractor_name=TOMAS,
                      items=[_item("Snapshot book", "O'Reilly Media", "75.00", "CUSTOM")])
    _approve(demo_client, str(kit["id"]))
    item_id = kit["items"][0]["id"]

    from app.db.models import KitItem

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        row = s.get(KitItem, uuid.UUID(item_id))
        assert row is not None
        row.amount = Decimal("76.00")
        s.commit()

    _assert_refusal(_purchase_item(demo_client, str(kit["id"]), item_id), "SNAPSHOT_MISMATCH")
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME


# ---------------------------------------------------------------------------
# 3. Per-item idempotency — bought once, debited once
# ---------------------------------------------------------------------------

def test_per_item_idempotency(demo_client):
    kit = _create_kit(demo_client, ACME, contractor_name=LENA,
                      items=[_item("Welcome gift card", "Amazon", "50.00", "GIFT_CARD")])
    _approve(demo_client, str(kit["id"]))
    item_id = kit["items"][0]["id"]

    first = _purchase_item(demo_client, str(kit["id"]), item_id)
    assert first.status_code == 200, first.text
    first = first.json()
    assert first["status"] == "SUCCEEDED", first
    assert _spent(demo_client, ACME) == "176.00"  # 126.00 seeded + 50.00, once

    second = _purchase_item(demo_client, str(kit["id"]), item_id)
    assert second.status_code == 200, second.text
    assert str(second.json()["id"]) == str(first["id"]), (first, second.json())
    assert _spent(demo_client, ACME) == "176.00"
    succeeded = [p for p in _purchases(demo_client, str(kit["id"])) if p["status"] == "SUCCEEDED"]
    assert len(succeeded) == 1, succeeded


# ---------------------------------------------------------------------------
# 4. Partial failure via TX limit -> PARTIALLY_PURCHASED
# ---------------------------------------------------------------------------

def test_partial_failure_tx_limit(demo_client):
    kit = _create_kit(demo_client, ACME, contractor_name=TOMAS, items=[
        _item("Monitor", "Dell", "250.00", "PERIPHERAL"),  # over the USD 200 test-card limit
        _item("Welcome gift card", "Amazon", "50.00", "GIFT_CARD"),
        _item("Background verification", "Checkr", "40.00", "BACKGROUND_CHECK"),
    ])
    _approve(demo_client, str(kit["id"]))

    r = demo_client.post(f"/onboarding/kits/{kit['id']}/purchase-all", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kit"]["status"] == "PARTIALLY_PURCHASED", body
    by_label = {i["label"]: i for i in body["kit"]["items"]}
    assert by_label["Monitor"]["status"] == "PENDING", by_label  # blocked, not declined
    assert by_label["Monitor"]["failure_code"] == "TX_LIMIT_EXCEEDED", by_label
    assert by_label["Welcome gift card"]["status"] == "PURCHASED", by_label
    assert by_label["Background verification"]["status"] == "PURCHASED", by_label
    failed = [p for p in body["purchases"] if p["status"] == "FAILED"]
    assert len(failed) == 1 and failed[0]["failure_code"] == "TX_LIMIT_EXCEEDED", body
    # Only the two successful items debited the budget.
    assert _spent(demo_client, ACME) == "216.00"  # 126 + 50 + 40

    # purchase-all again: PURCHASED items are never re-bought.
    r2 = demo_client.post(f"/onboarding/kits/{kit['id']}/purchase-all", json={})
    assert r2.status_code == 200, r2.text
    assert _spent(demo_client, ACME) == "216.00"
    succeeded = [p for p in _purchases(demo_client, str(kit["id"])) if p["status"] == "SUCCEEDED"]
    assert len(succeeded) == 2, succeeded


# ---------------------------------------------------------------------------
# 5. Held payroll locks onboarding purchases
# ---------------------------------------------------------------------------

def test_held_payroll_locks(demo_client):
    borealis = _employer_detail(demo_client, BOREALIS)
    run = demo_client.post(
        "/runs", json={"employer_id": borealis["id"],
                       "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    assert run["status"] == "HELD_SHORTFALL", run
    assert run["shortfall_usd"] == "2642.88", run

    kit = _create_kit(demo_client, BOREALIS, contractor_name=INGRID,
                      items=[_item("Welcome gift card", "Amazon", "50.00", "GIFT_CARD")])
    _approve(demo_client, str(kit["id"]))
    _assert_refusal(_purchase_item(demo_client, str(kit["id"]), kit["items"][0]["id"]), "PAYROLL_HELD")
    assert _spent(demo_client, BOREALIS) == "0.00"


# ---------------------------------------------------------------------------
# 6. Cross-employer isolation
# ---------------------------------------------------------------------------

def test_cross_employer_isolation(demo_client):
    # Cobalt's budget is DISABLED; Acme's enabled budget must not leak in.
    kit = _create_kit(demo_client, COBALT, contractor_name="Sam Lee",
                      items=[_item("Welcome gift card", "Amazon", "50.00", "GIFT_CARD")])
    _approve(demo_client, str(kit["id"]))
    _assert_refusal(_purchase_item(demo_client, str(kit["id"]), kit["items"][0]["id"]), "BUDGET_NOT_ENABLED")
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME

    acme_id = str(_employers(demo_client)[ACME]["id"])
    cobalt_id = str(_employers(demo_client)[COBALT]["id"])
    acme_kits = _kits(demo_client, employer_id=acme_id)
    assert acme_kits and all(str(k["employer_id"]) == acme_id for k in acme_kits), acme_kits
    assert all(str(k["id"]) != str(kit["id"]) for k in acme_kits), acme_kits
    cobalt_kits = _kits(demo_client, employer_id=cobalt_id)
    assert any(str(k["id"]) == str(kit["id"]) for k in cobalt_kits), cobalt_kits

    # A kit id that does not exist in this org is a 404, never a leak.
    r = demo_client.post(f"/onboarding/kits/{uuid.uuid4()}/decision", json={"decision": "approve"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# 7. Journal balanced, onboarding accounts only — never cash
# ---------------------------------------------------------------------------

def test_journal_onboarding_accounts_only(demo_client):
    amount = "80.00"
    kit = _create_kit(demo_client, ACME, contractor_name=TOMAS,
                      items=[_item("Headphones", "Sony", amount, "PERIPHERAL")])
    _approve(demo_client, str(kit["id"]))
    r = _purchase_item(demo_client, str(kit["id"]), kit["items"][0]["id"])
    assert r.status_code == 200, r.text
    purchase = r.json()
    assert purchase["status"] == "SUCCEEDED", purchase

    acme_id = str(_employers(demo_client)[ACME]["id"])
    journals = demo_client.get(f"/employers/{acme_id}/journals").json()
    txn = next((j for j in journals if str(j["id"]) == str(purchase["journal_txn_id"])), None)
    assert txn is not None, journals
    assert txn["balanced"] is True, txn
    assert any("onboarding_expense" in line["account"] and str(line.get("debit")) == amount
               for line in txn["lines"]), txn
    assert any("onboarding_budget" in line["account"] and str(line.get("credit")) == amount
               for line in txn["lines"]), txn
    assert all("cash" not in line["account"].split(":") for line in txn["lines"]), txn
    assert all("onboarding_" in line["account"] for line in txn["lines"]), txn
    total_debit = sum(Decimal(str(line.get("debit", "0"))) for line in txn["lines"])
    total_credit = sum(Decimal(str(line.get("credit", "0"))) for line in txn["lines"])
    assert total_debit == total_credit == Decimal(amount), txn


# ---------------------------------------------------------------------------
# 8. Monthly cap refusal
# ---------------------------------------------------------------------------

def test_monthly_cap_refusal(demo_client):
    original = _budget_for(demo_client, ACME)
    try:
        payload = {"currency": "USD", "monthly_cap": "150.00",
                   "per_tx_limit": original.get("per_tx_limit"), "enabled": True}
        r = demo_client.put(f"/onboarding/budgets/{original['employer_id']}", json=payload)
        assert r.status_code == 200, r.text
        # 126.00 already spent leaves 24.00; a 40.00 item cannot fit.
        kit = _create_kit(demo_client, ACME, contractor_name=LENA,
                          items=[_item("Background verification", "Checkr", "40.00", "BACKGROUND_CHECK")])
        _approve(demo_client, str(kit["id"]))
        _assert_refusal(_purchase_item(demo_client, str(kit["id"]), kit["items"][0]["id"]),
                        "MONTHLY_CAP_EXCEEDED")
        assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME
    finally:
        payload = {"currency": "USD", "monthly_cap": original["monthly_cap"],
                   "per_tx_limit": original.get("per_tx_limit"), "enabled": original["enabled"]}
        demo_client.put(f"/onboarding/budgets/{original['employer_id']}", json=payload)


# ---------------------------------------------------------------------------
# 9. Template drafting (WORKFLOW provenance) — service level
# ---------------------------------------------------------------------------

def test_draft_kit_from_template_idempotent(demo_client):
    from sqlalchemy import select

    from app.db.models import Contractor, Employer, OnboardingKit
    from app.services.onboarding import draft_kit_from_template

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        acme = s.scalar(select(Employer).where(Employer.name == ACME))
        tomas = s.scalar(select(Contractor).where(Contractor.employer_id == acme.id,
                                                  Contractor.full_name == TOMAS))
        kit = draft_kit_from_template(s, organization_id=acme.organization_id,
                                      employer=acme, contractor=tomas)
        s.commit()
        kit_id = kit.id
        assert kit.source == "WORKFLOW", kit.source
        # Contractor carries no role/title -> Notion seat only:
        # Background 40 + Notion 8 + Gift card 50 = 3 items, USD 98.00.
        assert kit.draft_reason == (
            "Drafted by workflow from Acme Manufacturing GmbH's standard kit template — "
            "3 items, USD 98.00"), kit.draft_reason
        assert kit.status == "DRAFT"
        assert len(kit.items) == 3
        assert sum(Decimal(str(i.amount)) for i in kit.items) == Decimal("98.00")
        # Idempotent: a second draft returns the SAME kit, no duplicate.
        again = draft_kit_from_template(s, organization_id=acme.organization_id,
                                        employer=acme, contractor=tomas)
        s.commit()
        assert again.id == kit_id
        count = len(s.scalars(select(OnboardingKit).where(
            OnboardingKit.contractor_id == tomas.id)).all())
        assert count == 1, count


def test_kit_provenance_via_api(demo_client):
    kit = _create_kit(demo_client, ACME, contractor_name=TOMAS,
                      items=[_item("Notion seat", "Notion", "8.00", "SAAS_SEAT")],
                      source="CHAT", draft_reason="Drafted from chat request")
    assert kit["source"] == "CHAT", kit
    assert kit["draft_reason"] == "Drafted from chat request", kit
