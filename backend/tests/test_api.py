"""End-to-end API: auth, tenant isolation, runs, TAP-signed decisions, persistence."""

from fastapi.testclient import TestClient

from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

SIGNUP = {
    "organization": {"name": "Northwind Staffing", "country": "gb", "base_currency": "usd",
                     "size_band": "11-50", "industry": "Staffing"},
    "admin": {"full_name": "Nadia Khan", "email": "nadia@northwind.io", "password": "Northwind2026"},
}


def _employers(c: TestClient) -> dict[str, dict]:
    return {e["name"]: e for e in c.get("/employers").json()}


def test_health_reports_partners(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["mode"] == "demo"
    assert body["partners"]["visa_tap"].startswith("signing")


def test_demo_credentials_and_login(client):
    creds = client.get("/auth/demo-credentials").json()
    assert creds["email"] == DEMO_EMAIL
    r = client.post("/auth/login", json={"email": DEMO_EMAIL.upper(), "password": DEMO_PASSWORD})
    assert r.status_code == 200
    assert r.json()["organization"]["name"] == "Vertex Payroll Services"
    assert "paysilo_session" in r.cookies
    assert client.get("/auth/me").json()["role"] == "owner"


def test_wrong_password_is_rejected_and_throttled(client):
    for _ in range(8):
        assert client.post("/auth/login", json={"email": DEMO_EMAIL, "password": "nope"}).status_code == 401
    r = client.post("/auth/login", json={"email": DEMO_EMAIL, "password": "nope"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_protected_routes_need_a_session(client):
    for path in ("/employers", "/runs", "/overview", "/auth/me"):
        assert client.get(path).status_code == 401


def test_signup_creates_org_owner_and_session(client):
    r = client.post("/auth/signup", json=SIGNUP)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["organization"]["slug"] == "northwind-staffing"
    assert body["organization"]["country"] == "GB" and body["role"] == "owner"
    assert client.get("/employers").json() == []  # a new org sees nothing of the demo org
    assert client.post("/auth/signup", json=SIGNUP).status_code == 409  # email taken


def test_signup_validates_password_and_fields(client):
    bad = {**SIGNUP, "admin": {**SIGNUP["admin"], "password": "short"}}
    assert client.post("/auth/signup", json=bad).status_code == 422
    bad = {**SIGNUP, "admin": {**SIGNUP["admin"], "password": "lettersonlyhere"}}
    assert client.post("/auth/signup", json=bad).status_code == 422
    bad = {**SIGNUP, "organization": {**SIGNUP["organization"], "country": "GBR"}}
    assert client.post("/auth/signup", json=bad).status_code == 422


def test_seeded_balances_come_from_the_ledger(demo_client):
    emps = _employers(demo_client)
    assert emps["Acme Manufacturing GmbH"]["balances"] == {"USD": "20000.00"}
    assert emps["Borealis Labs AB"]["balances"] == {"USD": "5000.00"}
    assert emps["Cobalt Retail LLC"]["balances"] == {}
    assert all(e["airwallex_account_id"] for e in emps.values())


def test_tenant_isolation_between_organizations(demo_client):
    acme_id = _employers(demo_client)["Acme Manufacturing GmbH"]["id"]
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json=SIGNUP)
    assert demo_client.get(f"/employers/{acme_id}").status_code == 404
    assert demo_client.post(f"/employers/{acme_id}/fund", json={"amount": "1"}).status_code == 404
    assert demo_client.post("/runs", json={"employer_id": acme_id, "instruction": "pay Lena Fischer EUR 1"}
                            ).status_code == 404


def test_contractor_of_another_employer_cannot_be_paid(demo_client):
    emps = _employers(demo_client)
    borealis = demo_client.get(f"/employers/{emps['Borealis Labs AB']['id']}").json()
    ingrid = borealis["contractors"][0]["id"]
    r = demo_client.post("/runs", json={"employer_id": emps["Acme Manufacturing GmbH"]["id"],
                                        "lines": [{"contractor_id": ingrid}]})
    assert r.status_code == 422
    assert "does not belong" in r.json()["detail"]


def test_acme_run_approve_settles_with_tap_signature_and_receipt(demo_client):
    acme = demo_client.get(f"/employers/{_employers(demo_client)['Acme Manufacturing GmbH']['id']}").json()
    lines = [{"contractor_id": c["id"]} for c in acme["contractors"]]
    run = demo_client.post("/runs", json={"employer_id": acme["id"], "lines": lines}).json()
    assert run["status"] == "RESERVED" and run["pending"]["kind"] == "approval" and run["resumable"]
    assert run["assessment"]["total_required"] == "6025.70"
    assert [p["status"] for p in run["pay_lines"]] == ["PENDING", "PENDING"]

    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED", done
    assert done["fee_charged_usd"] == "149.00"
    assert [p["status"] for p in done["pay_lines"]] == ["PAID", "PAID"]
    assert all(p["airwallex_transfer_id"] for p in done["pay_lines"])
    assert done["approvals"][0]["verified"] is True and done["approvals"][0]["tap_signature"]
    assert done["receipt"]["chain"] == "mock"
    assert {e["agent"] for e in done["events"]} >= {"intake", "price", "assess", "approval", "execute", "fee",
                                                     "reconcile", "attest"}
    # 20000 - 6507.76 converted - 149 fee
    assert _employers(demo_client)["Acme Manufacturing GmbH"]["balances"]["USD"] == "13343.24"
    overview = demo_client.get("/overview").json()
    assert overview["platform_fees"] == {"USD": "149.00"}
    assert overview["runs_settled"] == 1 and overview["runs_waiting"] == 0

    again = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
    assert again.status_code == 409  # a settled run cannot be decided twice


def test_borealis_is_held_and_cobalt_deposit_does_not_help(demo_client):
    emps = _employers(demo_client)
    borealis = demo_client.get(f"/employers/{emps['Borealis Labs AB']['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": borealis["id"],
                                          "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    assert run["status"] == "HELD_SHORTFALL" and run["pending"]["kind"] == "escalation"
    assert run["shortfall_usd"] == "2642.88"

    # Cobalt's USD 10,000 deposit lands; Borealis's wallet is unchanged.
    r = demo_client.post(f"/employers/{emps['Cobalt Retail LLC']['id']}/fund", json={"amount": "10000"})
    assert r.json()["balances"] == {"USD": "10000.00"}
    assert _employers(demo_client)["Borealis Labs AB"]["balances"] == {"USD": "5000.00"}

    assert demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).status_code == 409
    held = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "hold"}).json()
    assert held["status"] == "HELD_SHORTFALL" and held["fee_charged_usd"] == "0.00"
    assert [p["status"] for p in held["pay_lines"]] == ["HELD"]


def test_text_instruction_goes_through_intake_agent(demo_client):
    acme_id = _employers(demo_client)["Acme Manufacturing GmbH"]["id"]
    text = "employer: Acme Manufacturing GmbH\npay Lena Fischer EUR 3400\npay Mystery Person EUR 50"
    run = demo_client.post("/runs", json={"employer_id": acme_id, "instruction": text}).json()
    assert run["status"] == "REFUSED"  # Mystery Person has no bank details on file
    assert "Mystery Person" in run["error"]
    assert any("not in Acme Manufacturing GmbH's contractor directory" in e["message"] for e in run["events"])


def test_hold_decision_on_approval_moves_no_money(demo_client):
    acme = demo_client.get(f"/employers/{_employers(demo_client)['Acme Manufacturing GmbH']['id']}").json()
    run = demo_client.post("/runs", json={"employer_id": acme["id"],
                                          "lines": [{"contractor_id": acme["contractors"][0]["id"]}]}).json()
    held = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "hold"}).json()
    assert held["status"] == "HELD_BY_HUMAN"
    assert _employers(demo_client)["Acme Manufacturing GmbH"]["balances"] == {"USD": "20000.00"}


def test_duplicate_funding_reference_is_rejected(demo_client):
    cobalt_id = _employers(demo_client)["Cobalt Retail LLC"]["id"]
    ok = demo_client.post(f"/employers/{cobalt_id}/fund", json={"amount": "500", "reference": "inv-1"})
    assert ok.status_code == 200
    dup = demo_client.post(f"/employers/{cobalt_id}/fund", json={"amount": "500", "reference": "inv-1"})
    assert dup.status_code == 409
    assert _employers(demo_client)["Cobalt Retail LLC"]["balances"] == {"USD": "500.00"}


def test_create_employer_and_contractor_validation(demo_client):
    r = demo_client.post("/employers", json={"name": "Delta Foods", "country": "nl"})
    assert r.status_code == 201
    emp = r.json()
    assert emp["status"] == "ACTIVE" and emp["airwallex_account_id"]
    bad = demo_client.post(f"/employers/{emp['id']}/contractors",
                           json={"full_name": "No Bank", "country": "NL", "currency": "EUR"})
    assert bad.status_code == 422
    good = demo_client.post(f"/employers/{emp['id']}/contractors",
                            json={"full_name": "Joris de Vries", "country": "NL", "currency": "EUR",
                                  "iban": "NL91 ABNA 0417 1643 00", "swift_code": "ABNANL2A",
                                  "default_amount": "2500"})
    assert good.status_code == 201
    assert good.json()["iban_last4"] == "4300" and "iban" not in good.json()
    assert demo_client.post("/employers", json={"name": "Delta Foods", "country": "NL"}).status_code == 409


def test_agents_endpoint_lists_the_crew(client):
    crew = client.get("/agents").json()
    assert [a["id"] for a in crew][:3] == ["intake", "price", "assess"]
    assert sum(1 for a in crew if a["kind"] == "llm") == 1


def test_balances_and_history_survive_a_restart(tmp_path):
    from app.api import create_app
    from app.config import Settings
    from app.db import Database

    url = f"sqlite:///{(tmp_path / 'paysilo.db').as_posix()}"
    settings = Settings(database_url=url, jwt_secret="restart-secret-" + "x" * 32)
    login = {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}

    with TestClient(create_app(settings, Database.from_url(url))) as c:
        c.post("/auth/login", json=login)
        acme = c.get(f"/employers/{_employers(c)['Acme Manufacturing GmbH']['id']}").json()
        run = c.post("/runs", json={"employer_id": acme["id"],
                                    "lines": [{"contractor_id": x["id"]} for x in acme["contractors"]]}).json()
        c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"})
        paused = c.post("/runs", json={"employer_id": acme["id"],
                                       "lines": [{"contractor_id": acme["contractors"][0]["id"]}]}).json()

    with TestClient(create_app(settings, Database.from_url(url))) as c:  # "restart": seed must not re-run
        c.post("/auth/login", json=login)
        assert _employers(c)["Acme Manufacturing GmbH"]["balances"] == {"USD": "13343.24"}
        assert len(_employers(c)) == 4
        assert c.get(f"/runs/{run['id']}").json()["status"] == "SETTLED"
        stale = c.get(f"/runs/{paused['id']}").json()
        assert stale["resumable"] is False
        assert c.post(f"/runs/{paused['id']}/decision", json={"decision": "approve"}).status_code == 409
        # The rehydrated simulator still matches the ledger: a fresh run settles and reconciles.
        fresh = c.post("/runs", json={"employer_id": acme["id"],
                                      "lines": [{"contractor_id": acme["contractors"][1]["id"]}]}).json()
        done = c.post(f"/runs/{fresh['id']}/decision", json={"decision": "approve"}).json()
        assert done["status"] == "SETTLED", done["error"]


def test_airwallex_refusal_on_employer_creation_is_a_clear_502_and_saves_nothing(demo_client, monkeypatch):
    from app.airwallex.client import AirwallexError

    runtime = demo_client.app.state.runtime

    def refuse(*_a, **_k):
        raise AirwallexError("POST /accounts/create failed (HTTP 400, code=invalid_argument): "
                             "This account cannot onboard business accounts")

    monkeypatch.setattr(runtime.recorder.inner, "create_connected_account", refuse)
    r = demo_client.post("/employers", json={"name": "Echo Ltd", "country": "GB",
                                             "contact_email": "ops@echo.example"})
    assert r.status_code == 502
    assert "cannot onboard business accounts" in r.json()["detail"]
    assert "Echo Ltd" not in [e["name"] for e in demo_client.get("/employers").json()]
