"""Silo Benefits (VIC lane) — QA + accounting verification.

Written against the fixed benefits contract while the backend was built
in parallel, following the harness in ``tests/conftest.py`` (``client`` /
``demo_client``; demo login ``demo@paysilo.test``) and the flow patterns
in ``test_e2e_money.py`` / ``test_bedata.py``.

What is pinned here is money behaviour, not just response shapes:

- seed invariants: Acme has already spent USD 59.00 of a USD 500.00 cap
  (one PURCHASED, journal-backed request); Cobalt's budget is DISABLED;
- the monthly cap, the Airwallex test-card per-transaction limit
  (USD 200.00) and the disabled-budget gate each refuse a purchase with
  a 409 carrying ``{"detail": {"code": ..., "message": ...}}`` and move
  no money (``spent_this_period`` is unchanged after every refusal);
- the approval snapshot binds employer + merchant + item + amount +
  currency + expiry: tampering with the stored amount in the DB
  invalidates the approval (SNAPSHOT_MISMATCH);
- wages before perks: an employer whose payroll run is HELD
  (HELD_SHORTFALL, Borealis short USD 2,642.88) cannot purchase;
- a purchase is idempotent: a retry returns the SAME purchase (same id,
  same provider_ref) and the budget is debited exactly once;
- a successful purchase posts a BALANCED journal to the employer's
  ``benefits_expense`` / ``benefits_budget`` accounts — never cash;
- VIC misconfiguration fails closed (VIC_UNAVAILABLE) and never falls
  back to a simulated purchase;
- the purchase door requires an authenticated decider (401 anonymous).

Golden numbers that must not move (demo seed): Acme settles payroll at
USD 13,343.24 with a USD 149.00 fee; Borealis is held short USD 2,642.88.

Isolation note: the ``client`` fixture in conftest.py is function-scoped
and builds a fresh in-memory SQLite app per test, so budget mutations
and DB tampering inside one test cannot poison another. Budget PUTs are
still restored in ``finally`` blocks where a test changes them, so the
suite stays correct even if the fixture scope ever widens.
"""

from __future__ import annotations

import inspect
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

ACME = "Acme Manufacturing GmbH"
BOREALIS = "Borealis Labs AB"
COBALT = "Cobalt Retail LLC"

LENA = "Lena Fischer"
TOMAS = "Tomas Rivera"
INGRID = "Ingrid Solberg"

SEEDED_SPENT_ACME = "59.00"  # Tomas Rivera / O'Reilly Media / 59.00, PURCHASED


# ---------------------------------------------------------------------------
# Helpers — employers / budgets / requests / purchases via the public API
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
    detail = _employer_detail(c, employer_name)
    for contractor in detail.get("contractors", []):
        if contractor.get("full_name") == person_name or contractor.get("name") == person_name:
            return str(contractor["id"])
    return None


def _budgets(c: TestClient) -> list[dict]:
    r = c.get("/benefits/budgets")
    assert r.status_code == 200, r.text
    return r.json()


def _budget_for(c: TestClient, employer_name: str) -> dict:
    for row in _budgets(c):
        if row.get("employer_name") == employer_name:
            return row
    raise AssertionError(f"no benefit budget listed for {employer_name!r}: {_budgets(c)}")


def _put_budget(c: TestClient, budget_row: dict, *, check: bool = True, **overrides) -> dict:
    """PUT a full budget row (contract body) with selected fields changed."""
    payload = {
        "currency": overrides.get("currency", budget_row.get("currency", "USD")),
        "monthly_cap": overrides.get("monthly_cap", budget_row.get("monthly_cap")),
        "per_tx_limit": overrides.get("per_tx_limit", budget_row.get("per_tx_limit")),
        "enabled": overrides.get("enabled", budget_row.get("enabled")),
    }
    r = c.put(f"/benefits/budgets/{budget_row['employer_id']}", json=payload)
    if check:
        assert r.status_code == 200, r.text
        return r.json()
    return {}


def _restore_budget(c: TestClient, original_row: dict) -> None:
    _put_budget(c, original_row, check=False,
                currency=original_row.get("currency", "USD"),
                monthly_cap=original_row.get("monthly_cap"),
                per_tx_limit=original_row.get("per_tx_limit"),
                enabled=original_row.get("enabled"))


def _spent(c: TestClient, employer_name: str) -> str:
    return str(_budget_for(c, employer_name)["spent_this_period"])


def _requests(c: TestClient, employer_id: str | None = None) -> list[dict]:
    params = {"employer_id": employer_id} if employer_id else None
    r = c.get("/benefits/requests", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _contractor_name_of(row: dict) -> str | None:
    if row.get("contractor_name"):
        return row["contractor_name"]
    nested = row.get("contractor") or {}
    return nested.get("full_name") or nested.get("name")


def _find_request(rows: list[dict], *, contractor_name: str | None = None,
                  merchant: str | None = None, status: str | None = None,
                  employer_name: str | None = None, amount: str | None = None,
                  request_id: str | None = None) -> dict:
    for row in rows:
        if request_id is not None and str(row.get("id")) != str(request_id):
            continue
        if contractor_name is not None and _contractor_name_of(row) != contractor_name:
            continue
        if merchant is not None and row.get("merchant") != merchant:
            continue
        if status is not None and row.get("status") != status:
            continue
        if employer_name is not None and row.get("employer_name") not in (None, employer_name):
            # Rows that carry an employer_name must match it; rows that only
            # carry employer_id are matched by the caller via filtered GETs.
            if row.get("employer_name") != employer_name:
                continue
        if amount is not None and str(row.get("amount")) != amount:
            continue
        return row
    raise AssertionError(
        f"benefit request not found (contractor={contractor_name!r}, merchant={merchant!r}, "
        f"status={status!r}, amount={amount!r}, id={request_id!r}) in {rows}")


def _seeded_request(c: TestClient, contractor_name: str, **kwargs) -> dict:
    return _find_request(_requests(c), contractor_name=contractor_name, **kwargs)


def _create_request(c: TestClient, employer_name: str, *, merchant: str, item: str,
                    amount: str, contractor_name: str, currency: str = "USD") -> dict:
    employer_id = str(_employers(c)[employer_name]["id"])
    payload: dict = {
        "employer_id": employer_id,
        "merchant": merchant,
        "item": item,
        "amount": amount,
        "currency": currency,
        "contractor_name": contractor_name,
    }
    contractor_id = _contractor_id(c, employer_name, contractor_name)
    if contractor_id is not None:
        payload["contractor_id"] = contractor_id
    r = c.post("/benefits/requests", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert str(body.get("amount")) == amount, body
    assert body.get("status") in ("REQUESTED", None) or body.get("status") == "REQUESTED", body
    return body


def _approve(c: TestClient, request_id: str) -> dict:
    r = c.post(f"/benefits/requests/{request_id}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    body = r.json()
    if isinstance(body, dict) and body.get("status") is not None:
        assert body["status"] == "APPROVED", body
    return body


def _purchase(c: TestClient, request_id: str):
    return c.post(f"/benefits/requests/{request_id}/purchase", json={})


def _purchase_ok(c: TestClient, request_id: str) -> dict:
    r = _purchase(c, request_id)
    assert r.status_code == 200, r.text
    body = r.json()
    if isinstance(body, dict) and body.get("status") is not None:
        assert body["status"] == "SUCCEEDED", body
    return body


def _assert_policy_refusal(response, code: str) -> dict:
    """Policy refusals are 409 with {"detail": {"code", "message"}} — pinned."""
    assert response.status_code == 409, response.text
    body = response.json()
    detail = body.get("detail")
    assert isinstance(detail, dict), f"refusal detail must be an object with code/message: {body}"
    assert detail.get("code") == code, body
    assert detail.get("message"), body
    return detail


def _purchases(c: TestClient) -> list[dict]:
    r = c.get("/benefits/purchases")
    assert r.status_code == 200, r.text
    return r.json()


def _purchases_for(c: TestClient, request_id: str) -> list[dict]:
    return [p for p in _purchases(c) if str(p.get("request_id")) == str(request_id)]


def _status_body(c: TestClient) -> dict:
    r = c.get("/benefits/status")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. Seed state
# ---------------------------------------------------------------------------

def test_seed_state(demo_client):
    budgets = {b["employer_name"]: b for b in _budgets(demo_client)}
    assert set(budgets) >= {ACME, BOREALIS, COBALT}, budgets

    acme = budgets[ACME]
    assert str(acme["monthly_cap"]) == "500.00", acme
    assert str(acme["spent_this_period"]) == SEEDED_SPENT_ACME, acme
    assert str(acme["remaining"]) == "441.00", acme
    assert acme["enabled"] is True, acme
    assert budgets[BOREALIS]["enabled"] is True, budgets[BOREALIS]
    assert str(budgets[BOREALIS]["monthly_cap"]) == "500.00", budgets[BOREALIS]

    cobalt = budgets[COBALT]
    assert cobalt["enabled"] is False, cobalt
    assert str(cobalt["monthly_cap"]) == "250.00", cobalt

    requests = _requests(demo_client)
    lena = _find_request(requests, contractor_name=LENA, status="REQUESTED")
    assert str(lena["amount"]) == "149.00", lena
    assert lena.get("merchant") == "Coursera", lena

    tomas = _find_request(requests, contractor_name=TOMAS, status="PURCHASED")
    assert str(tomas["amount"]) == SEEDED_SPENT_ACME, tomas
    assert tomas.get("merchant") == "O'Reilly Media", tomas

    ingrid = _find_request(requests, contractor_name=INGRID, status="REQUESTED")
    assert str(ingrid["amount"]) == "120.00", ingrid

    # The seeded Tomas purchase is a real SIMULATED purchase row, not a number
    # typed into the budget: exactly one SUCCEEDED purchase backs the 59.00.
    seeded = [p for p in _purchases_for(demo_client, str(tomas["id"])) if p.get("status") == "SUCCEEDED"]
    assert len(seeded) == 1, seeded
    assert str(seeded[0]["amount"]) == SEEDED_SPENT_ACME, seeded[0]
    if seeded[0].get("mode") is not None:
        assert seeded[0]["mode"] == "SIMULATED", seeded[0]

    status = _status_body(demo_client)
    assert status["mode"] == "SIMULATED", status
    assert str(status["tx_limit_usd"]) == "200.00", status
    assert "credential_ref" in status and "test_card" in status and "cli_available" in status, status


# ---------------------------------------------------------------------------
# 2. Monthly cap enforcement
# ---------------------------------------------------------------------------

def test_monthly_cap_enforcement(demo_client):
    original = _budget_for(demo_client, ACME)
    try:
        # Cap 100.00 with 59.00 already spent leaves 41.00 of headroom;
        # Lena's approved 149.00 cannot fit, however valid otherwise.
        _put_budget(demo_client, original, monthly_cap="100.00")
        assert str(_budget_for(demo_client, ACME)["monthly_cap"]) == "100.00"

        lena = _seeded_request(demo_client, LENA, status="REQUESTED")
        _approve(demo_client, str(lena["id"]))

        _assert_policy_refusal(_purchase(demo_client, str(lena["id"])), "MONTHLY_CAP_EXCEEDED")

        # A refusal moves no money and records no successful purchase.
        assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME
        assert [p for p in _purchases_for(demo_client, str(lena["id"]))
                if p.get("status") == "SUCCEEDED"] == []
        after = _find_request(_requests(demo_client), request_id=str(lena["id"]))
        assert after["status"] != "PURCHASED", after
    finally:
        _restore_budget(demo_client, original)


# ---------------------------------------------------------------------------
# 3. Test-card per-transaction limit
# ---------------------------------------------------------------------------

def test_test_card_tx_limit(demo_client):
    original = _budget_for(demo_client, ACME)
    try:
        # Raise the monthly cap AND the budget's own per-tx limit well above
        # 250.00, so the ONLY limit that can bind is the published Airwallex
        # test card's USD 200.00 ceiling behind VIC:
        # effective per-tx limit = min(budget per_tx_limit, 200.00).
        _put_budget(demo_client, original, monthly_cap="1000.00", per_tx_limit="500.00")

        req = _create_request(
            demo_client, ACME, merchant="Coursera", item="Tx-limit probe course",
            amount="250.00", contractor_name=LENA)
        _approve(demo_client, str(req["id"]))

        _assert_policy_refusal(_purchase(demo_client, str(req["id"])), "TX_LIMIT_EXCEEDED")

        assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME
        assert [p for p in _purchases_for(demo_client, str(req["id"]))
                if p.get("status") == "SUCCEEDED"] == []
    finally:
        _restore_budget(demo_client, original)


# ---------------------------------------------------------------------------
# 4. Approval snapshot tamper invalidates the approval
# ---------------------------------------------------------------------------

def test_snapshot_tamper_invalidates(demo_client):
    # A request created by THIS test (not a seeded one) is tampered with, so
    # the suite stays correct even if the DB fixture scope ever widens and
    # seeded rows become shared across tests.
    req = _create_request(
        demo_client, ACME, merchant="O'Reilly Media", item="Snapshot integrity book",
        amount="75.00", contractor_name=TOMAS)
    _approve(demo_client, str(req["id"]))

    # The API offers no way to edit an approved request's amount — tamper
    # directly in the DB, the way a buggy writer or an attacker with DB
    # access would. The snapshot hash bound at approval must catch it.
    from app.db.models import BenefitRequest

    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        row = s.get(BenefitRequest, uuid.UUID(str(req["id"])))
        assert row is not None, "created request must exist in the DB"
        row.amount = Decimal("76.00")
        s.commit()

    _assert_policy_refusal(_purchase(demo_client, str(req["id"])), "SNAPSHOT_MISMATCH")

    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME
    assert [p for p in _purchases_for(demo_client, str(req["id"]))
            if p.get("status") == "SUCCEEDED"] == []


# ---------------------------------------------------------------------------
# 5. Held payroll locks benefits (wages first)
# ---------------------------------------------------------------------------

def test_held_payroll_locks_benefits(demo_client):
    borealis = _employer_detail(demo_client, BOREALIS)
    run = demo_client.post(
        "/runs", json={"employer_id": borealis["id"],
                       "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    # Golden shortfall: Borealis is held, short exactly USD 2,642.88.
    assert run["status"] == "HELD_SHORTFALL", run
    assert run["shortfall_usd"] == "2642.88", run

    # The budgets list surfaces the lock on Borealis itself.
    locked = _budget_for(demo_client, BOREALIS)
    assert locked["locked"] is True, locked
    assert locked.get("lock_reason"), locked

    spent_before = _spent(demo_client, BOREALIS)
    ingrid = _seeded_request(demo_client, INGRID, status="REQUESTED")
    _approve(demo_client, str(ingrid["id"]))

    _assert_policy_refusal(_purchase(demo_client, str(ingrid["id"])), "PAYROLL_HELD")

    assert _spent(demo_client, BOREALIS) == spent_before
    assert [p for p in _purchases_for(demo_client, str(ingrid["id"]))
            if p.get("status") == "SUCCEEDED"] == []


# ---------------------------------------------------------------------------
# 6. Idempotent purchase retry — debited exactly once
# ---------------------------------------------------------------------------

def test_idempotent_purchase_retry(demo_client):
    lena = _seeded_request(demo_client, LENA, status="REQUESTED")
    _approve(demo_client, str(lena["id"]))

    first = _purchase_ok(demo_client, str(lena["id"]))
    assert first.get("provider_ref"), first
    if first.get("mode") is not None:
        assert first["mode"] == "SIMULATED", first
    assert _spent(demo_client, ACME) == "208.00"  # 59.00 seeded + 149.00, once

    second = _purchase_ok(demo_client, str(lena["id"]))
    assert str(second["id"]) == str(first["id"]), (first, second)
    assert second.get("provider_ref") == first.get("provider_ref"), (first, second)

    # Still exactly one debit: 59.00 + 149.00 = 208.00, and exactly one
    # SUCCEEDED purchase row for this request.
    assert _spent(demo_client, ACME) == "208.00"
    succeeded = [p for p in _purchases_for(demo_client, str(lena["id"]))
                 if p.get("status") == "SUCCEEDED"]
    assert len(succeeded) == 1, succeeded
    assert str(succeeded[0]["id"]) == str(first["id"]), succeeded


# ---------------------------------------------------------------------------
# 7. Simulated VIC adapter determinism (unit)
# ---------------------------------------------------------------------------
# FRAGILITY NOTE for the coordinator: the fixed contract names the class
# (app.partners.vic.SimulatedVicAdapter) and its behaviours, but not the
# exact method signature. The helpers below therefore dispatch by
# parameter NAME (inspect.signature) across the plausible method names
# and accept a result as a dict, a dataclass-like object, or a tuple.
# If the merged backend uses a single canonical signature, these helpers
# still call it correctly; if a behaviour assertion fails, the failure is
# a real contract failure, not a calling-convention artefact.

def _make_simulated_adapter():
    from app.partners.vic import SimulatedVicAdapter

    try:
        return SimulatedVicAdapter()
    except TypeError:
        pass
    # Constructor variants: pass only the parameters the class accepts.
    candidates = {
        "credential_ref": "simulated-test-card",
        "test_card": "simulated-test-card",
        "credential": "simulated-test-card",
        "mode": "SIMULATED",
    }
    params = inspect.signature(SimulatedVicAdapter).parameters
    kwargs = {k: v for k, v in candidates.items() if k in params}
    return SimulatedVicAdapter(**kwargs)


def _adapter_result_field(result, *names):
    if isinstance(result, dict):
        for name in names:
            if result.get(name) is not None:
                return result[name]
        return None
    for name in names:
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None


def _adapter_ok(result) -> bool:
    if isinstance(result, tuple) and result and isinstance(result[0], bool):
        return result[0]
    ok = _adapter_result_field(result, "ok", "success", "succeeded")
    if ok is not None:
        return bool(ok)
    status = _adapter_result_field(result, "status")
    if status is not None:
        return str(status).upper() in ("SUCCEEDED", "SUCCESS", "OK", "APPROVED")
    raise AssertionError(f"cannot determine ok/success from adapter result: {result!r}")


def _adapter_code(result):
    if isinstance(result, tuple) and len(result) >= 3:
        return result[2]
    return _adapter_result_field(
        result, "code", "failure_code", "error_code", "decline_code", "reason_code")


def _adapter_provider_ref(result):
    if isinstance(result, tuple) and len(result) >= 2:
        return result[1]
    return _adapter_result_field(
        result, "provider_ref", "provider_reference", "reference", "ref", "transaction_ref")


def _call_adapter(adapter, *, merchant: str, amount: str, idempotency_key: str,
                  item: str = "Professional course", currency: str = "USD"):
    values = {
        "merchant": merchant,
        "item": item,
        "product": item,
        "description": item,
        "currency": currency,
        "idempotency_key": idempotency_key,
        "request_id": idempotency_key,
        "contractor_name": LENA,
        "beneficiary_name": LENA,
        "credential_ref": "simulated-test-card",
        "test_card": "simulated-test-card",
    }
    last_error: Exception | None = None
    for method_name in ("purchase", "buy", "charge", "execute_purchase", "execute", "pay"):
        method = getattr(adapter, method_name, None)
        if method is None or not callable(method):
            continue
        params = inspect.signature(method).parameters
        # Amount representation follows the declared annotation when there
        # is one; accounting code in this repo is Decimal-first.
        amount_value: object = Decimal(amount)
        if "amount" in params:
            annotation = str(params["amount"].annotation)
            if "str" in annotation:
                amount_value = amount
            elif "float" in annotation:
                amount_value = float(amount)
        kwargs = {}
        for name, param in params.items():
            if name == "amount":
                kwargs[name] = amount_value
            elif name in values:
                kwargs[name] = values[name]
            elif param.default is inspect.Parameter.empty:
                kwargs[name] = values.get(name, "test")
        try:
            return method(**kwargs)
        except TypeError as exc:  # calling-convention mismatch: try next shape
            last_error = exc
            # Single-positional-object convention: purchase({...request dict...})
            try:
                return method({**values, "amount": amount_value})
            except TypeError as exc2:
                last_error = exc2
                continue
    raise AssertionError(f"no callable purchase method on SimulatedVicAdapter worked: {last_error}")


def test_simulated_adapter_determinism():
    key = "benefits-unit-key-0001"
    first = _call_adapter(_make_simulated_adapter(), merchant="Coursera",
                          amount="50.00", idempotency_key=key)
    assert _adapter_ok(first) is True, first
    ref_first = _adapter_provider_ref(first)
    assert ref_first, first

    # A second, independent instance derives the SAME provider_ref from the
    # same idempotency key — no randomness, no per-instance state.
    second = _call_adapter(_make_simulated_adapter(), merchant="Coursera",
                           amount="50.00", idempotency_key=key)
    assert _adapter_ok(second) is True, second
    assert _adapter_provider_ref(second) == ref_first, (first, second)

    # A different key is a different purchase.
    other = _call_adapter(_make_simulated_adapter(), merchant="Coursera",
                          amount="50.00", idempotency_key="benefits-unit-key-0002")
    assert _adapter_provider_ref(other) != ref_first, (first, other)

    declined = _call_adapter(_make_simulated_adapter(), merchant="Decline Corp",
                             amount="50.00", idempotency_key="benefits-unit-key-decline")
    assert _adapter_ok(declined) is False, declined
    assert _adapter_code(declined) == "PROVIDER_DECLINED", declined

    too_big = _call_adapter(_make_simulated_adapter(), merchant="Coursera",
                            amount="250.00", idempotency_key="benefits-unit-key-big")
    assert _adapter_ok(too_big) is False, too_big
    assert _adapter_code(too_big) == "TX_LIMIT_EXCEEDED", too_big


# ---------------------------------------------------------------------------
# 8. Cross-employer budget isolation
# ---------------------------------------------------------------------------

def test_cross_employer_budget_isolation(demo_client):
    # Cobalt's own budget is DISABLED. Enabled budgets at Acme/Borealis must
    # not leak into Cobalt's silo: the gate reads the REQUEST's employer.
    req = _create_request(
        demo_client, COBALT, merchant="Coursera", item="Retail skills course",
        amount="50.00", contractor_name="Sam Lee")
    _approve(demo_client, str(req["id"]))

    _assert_policy_refusal(_purchase(demo_client, str(req["id"])), "BUDGET_NOT_ENABLED")
    assert [p for p in _purchases_for(demo_client, str(req["id"]))
            if p.get("status") == "SUCCEEDED"] == []
    # Nobody else's spent moved either.
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME

    # employer_id filtering on the requests list is a hard silo boundary.
    acme_id = str(_employers(demo_client)[ACME]["id"])
    cobalt_id = str(_employers(demo_client)[COBALT]["id"])
    acme_requests = _requests(demo_client, employer_id=acme_id)
    assert acme_requests, "Acme must have seeded requests"
    assert all(str(r["employer_id"]) == acme_id for r in acme_requests), acme_requests
    assert all(str(r["id"]) != str(req["id"]) for r in acme_requests), acme_requests

    cobalt_requests = _requests(demo_client, employer_id=cobalt_id)
    assert all(str(r["employer_id"]) == cobalt_id for r in cobalt_requests), cobalt_requests
    assert any(str(r["id"]) == str(req["id"]) for r in cobalt_requests), cobalt_requests


# ---------------------------------------------------------------------------
# 9. Journal posted and balanced — benefits accounts, never cash
# ---------------------------------------------------------------------------

def test_journal_posted_and_balanced(demo_client):
    amount = "80.00"  # distinct from the seeded 59.00 so the journal is unambiguous
    req = _create_request(
        demo_client, ACME, merchant="O'Reilly Media", item="Accounting reference book",
        amount=amount, contractor_name=TOMAS)
    _approve(demo_client, str(req["id"]))
    purchase = _purchase_ok(demo_client, str(req["id"]))

    acme_id = str(_employers(demo_client)[ACME]["id"])
    r = demo_client.get(f"/employers/{acme_id}/journals")
    assert r.status_code == 200, r.text
    journals = r.json()
    assert journals, "a settled employer must have journals"

    # Prefer the exact transaction the purchase points at via
    # journal_txn_id; fall back to finding the transaction that carries a
    # benefits_expense line for this amount (the content assertions below
    # are the strict part either way).
    txn = None
    journal_txn_id = purchase.get("journal_txn_id")
    if journal_txn_id is not None:
        match = [j for j in journals if str(j["id"]) == str(journal_txn_id)]
        if match:
            txn = match[0]
    if txn is None:
        for j in journals:
            if any("benefits_expense" in line["account"] and str(line.get("debit")) == amount
                   for line in j["lines"]):
                txn = j
                break
        assert txn is not None, f"no journal with a benefits_expense debit of {amount}: {journals}"

    if txn.get("employer_id") is not None:
        assert str(txn["employer_id"]) == acme_id, txn
    assert txn["balanced"] is True, txn

    expense = [line for line in txn["lines"] if "benefits_expense" in line["account"]]
    assert expense and any(str(line.get("debit")) == amount for line in expense), txn
    budget_lines = [line for line in txn["lines"] if "benefits_budget" in line["account"]]
    assert budget_lines and any(str(line.get("credit")) == amount for line in budget_lines), txn

    # Benefits are paid out of the employer's benefits budget, NEVER out of
    # the payroll cash account: no cash line may appear in this transaction.
    assert all("cash" not in line["account"].split(":") for line in txn["lines"]), txn

    # The journal balances to the cent, independently recomputed.
    total_debit = sum(Decimal(str(line.get("debit", "0"))) for line in txn["lines"])
    total_credit = sum(Decimal(str(line.get("credit", "0"))) for line in txn["lines"])
    assert total_debit == total_credit == Decimal(amount), txn


# ---------------------------------------------------------------------------
# 10. VIC unavailable fails closed — no simulated fallback
# ---------------------------------------------------------------------------

def test_vic_unavailable_fails_closed():
    # A second app, built exactly the way conftest builds the first, but
    # with a VIC credential configured and the airi CLI missing: VIC mode
    # was requested, cannot run, and must fail CLOSED. (If second-app
    # construction ever proves too fragile in the merged tree, the
    # fallback for this test is adapter-level: AiriVicAdapter.purchase
    # must raise VicUnavailable for a missing CLI — do not weaken this
    # test to a skip.)
    from app.api import create_app
    from app.config import Settings
    from app.db import Database
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    settings = Settings(
        database_url="sqlite://", jwt_secret="test-secret-" + "x" * 32, seed_demo=True,
        vic_credential="test-token", airi_cli_path="/nonexistent/airi-cli")
    app = create_app(settings, Database.from_url("sqlite://"))
    with TestClient(app) as c:
        login = c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        assert login.status_code == 200, login.text

        status = _status_body(c)
        assert status["mode"] == "VIC_UNAVAILABLE", status
        if status.get("cli_available") is not None:
            assert status["cli_available"] is False, status

        lena = _seeded_request(c, LENA, status="REQUESTED")
        _approve(c, str(lena["id"]))

        # In this app even the seeded historical purchase cannot execute
        # (fail-closed), so Acme's spent here is whatever the seed left —
        # the invariant is that the refused purchase does not move it.
        spent_before = _spent(c, ACME)

        _assert_policy_refusal(_purchase(c, str(lena["id"])), "VIC_UNAVAILABLE")

        # Fail-closed means fail-closed: nothing was executed as SIMULATED
        # behind the user's back — no purchase row at all for this request,
        # no spend, and the request was not marked PURCHASED.
        assert _purchases_for(c, str(lena["id"])) == []
        assert _spent(c, ACME) == spent_before
        after = _find_request(_requests(c), request_id=str(lena["id"]))
        assert after["status"] == "APPROVED", after


# ---------------------------------------------------------------------------
# 11. Purchase requires an authenticated decider
# ---------------------------------------------------------------------------

def test_agent_cannot_purchase(demo_client):
    # demo_client is signed in as the demo OWNER (a decider role), so role
    # denial cannot be exercised from that session. What this lane pins is
    # the unauthenticated boundary: there is no anonymous path to the one
    # money-moving benefits door. (The structural absence of an
    # approve/purchase tool on the chat/agent surface is covered by the
    # chat lane's tests, mirroring test_e2e_money's registry assertion.)
    lena = _seeded_request(demo_client, LENA, status="REQUESTED")

    anon = TestClient(demo_client.app)  # fresh client, no session cookie
    r = anon.post(f"/benefits/requests/{lena['id']}/purchase", json={})
    assert r.status_code == 401, r.text

    # The anonymous attempt changed nothing.
    after = _find_request(_requests(demo_client), request_id=str(lena["id"]))
    assert after["status"] == "REQUESTED", after
    assert _purchases_for(demo_client, str(lena["id"])) == []
    assert _spent(demo_client, ACME) == SEEDED_SPENT_ACME
