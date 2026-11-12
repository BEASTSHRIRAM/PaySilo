"""Contractor trust: bank-change control, onboarding links, tax/compliance tracking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app.airwallex.client import AirwallexClient
from app.airwallex.simulator import AirwallexSimulator
from app.config import Settings
from app.db import Database
from app.db.models import (
    Contractor,
    ContractorChange,
    ContractorOnboardingInvite,
    Employer,
    Organization,
    PayLine,
    PayrollRun,
)
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

LENA_IBAN = "DE89370400440532013000"
NEW_IBAN = "NL91ABNA0417164300"


def _employers(c):
    return {e["name"]: e for e in c.get("/employers").json()}


def _ctx(demo_client, employer="Acme Manufacturing GmbH", name="Lena Fischer"):
    emp = _employers(demo_client)[employer]
    detail = demo_client.get(f"/employers/{emp['id']}").json()
    c = next(x for x in detail["contractors"] if x["full_name"] == name)
    return emp["id"], c["id"]


def _login(c, email, password):
    c.post("/auth/logout")
    assert c.post("/auth/login", json={"email": email, "password": password}).status_code == 200


def _second_user(demo_client, role="admin", email="second@example.com"):
    inv = demo_client.post("/org/invitations", json={"email": email, "role": role}).json()
    token = inv["invite_path"].split("/")[-1]
    assert demo_client.post(f"/invitations/{token}/accept",
                            json={"full_name": "Second User", "password": "SecondPass2026"}).status_code == 200
    _login(demo_client, DEMO_EMAIL, DEMO_PASSWORD)
    return email, "SecondPass2026"


def _anon(client) -> TestClient:
    return TestClient(client.app)  # no cookies: the public, signed-out visitor


def _db(client):
    return client.app.state.db.sessionmaker()


def _change_bank(demo_client, eid, cid, iban=NEW_IBAN, swift="ABNANL2AXXX"):
    return demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"iban": iban, "swift_code": swift})


# ---------------------------------------------------------------- change control
def test_bank_edit_goes_pending_with_masked_log_and_validation(demo_client):
    eid, cid = _ctx(demo_client)
    r = _change_bank(demo_client, eid, cid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bank_change_pending"] is True
    assert body["contractor"]["bank_verification_status"] == "PENDING_VERIFICATION"
    assert body["contractor"]["iban_last4"] == "4300"
    assert body["bank_validation"] == {"valid": True, "checked": True, "errors": []}
    assert body["contractor"]["bank_details_changed_at"]
    hist = demo_client.get(f"/employers/{eid}/contractors/{cid}/history").json()
    bank = next(ch for ch in hist["changes"] if ch["group"] == "bank")
    assert "IBAN ****3000" in bank["summary"] and "****4300" in bank["summary"]
    assert bank["changed_by"] == "Priya Raman"
    blob = r.text + json.dumps(hist)
    assert LENA_IBAN not in blob and NEW_IBAN not in blob  # never full bank values


def test_profile_and_tax_edits_do_not_trigger_verification(demo_client):
    eid, cid = _ctx(demo_client, name="Tomas Rivera")
    r = demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={
        "full_name": "Tomas Rivera Ruiz", "tax_status": "NON_US", "tax_form_type": "W8BEN",
        "tax_form_status": "RECEIVED", "tax_form_expires_at": str(date.today() + timedelta(days=900))})
    assert r.status_code == 200, r.text
    assert r.json()["bank_change_pending"] is False
    assert r.json()["contractor"]["tax_form_status"] == "RECEIVED"
    assert r.json()["contractor"]["tax_form_received_at"] == str(date.today())  # defaulted
    groups = {c["group"] for c in demo_client.get(f"/employers/{eid}/contractors/{cid}/history").json()["changes"]}
    assert groups == {"profile", "tax"}


def test_edit_validation_and_received_needs_form_type(demo_client):
    eid, cid = _ctx(demo_client)
    assert demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"iban": "short"}).status_code == 422
    assert demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"swift_code": ""}).status_code == 422  # SWIFT needs BIC
    eid2, tomas = _ctx(demo_client, name="Tomas Rivera")  # no form type yet
    assert demo_client.patch(f"/employers/{eid2}/contractors/{tomas}", json={"tax_form_status": "RECEIVED"}).status_code == 422
    assert demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"bogus": 1}).status_code == 422


def test_same_user_cannot_verify_and_callback_required(demo_client):
    eid, cid = _ctx(demo_client)
    _change_bank(demo_client, eid, cid)
    url = f"/employers/{eid}/contractors/{cid}/verify-bank"
    r = demo_client.post(url, json={"callback_confirmed": True})
    assert r.status_code == 403 and "second person" in r.json()["detail"]
    assert demo_client.post(url, json={"callback_confirmed": False}).status_code == 422
    assert demo_client.post(url, json={}).status_code == 422
    email, pw = _second_user(demo_client)
    _login(demo_client, email, pw)
    assert demo_client.post(url, json={"callback_confirmed": False}).status_code == 422
    ok = demo_client.post(url, json={"callback_confirmed": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["bank_verification_status"] == "VERIFIED" and ok.json()["bank_verified_at"]
    assert demo_client.post(url, json={"callback_confirmed": True}).status_code == 409  # nothing pending now
    hist = demo_client.get(f"/employers/{eid}/contractors/{cid}/history").json()
    assert any("verified" in ch["summary"].lower() and ch["changed_by"] == "Second User" for ch in hist["changes"])


def test_verify_rejects_invalid_details(demo_client):
    eid, cid = _ctx(demo_client)
    r = _change_bank(demo_client, eid, cid, iban="DE89-3704-0044-0532-0130")  # hyphens: malformed
    assert r.status_code == 200 and r.json()["bank_validation"]["valid"] is False
    assert r.json()["bank_validation"]["errors"] == ["beneficiary.bank_details.iban"]
    email, pw = _second_user(demo_client)
    _login(demo_client, email, pw)
    v = demo_client.post(f"/employers/{eid}/contractors/{cid}/verify-bank", json={"callback_confirmed": True})
    assert v.status_code == 422 and "iban" in v.json()["detail"]
    assert demo_client.get(f"/employers/{eid}").json()["contractors"][0]["bank_verification_status"] in ("PENDING_VERIFICATION", "VERIFIED")
    c = next(x for x in demo_client.get(f"/employers/{eid}").json()["contractors"] if x["id"] == cid)
    assert c["bank_verification_status"] == "PENDING_VERIFICATION"


def test_role_gating(demo_client):
    eid, cid = _ctx(demo_client)
    _change_bank(demo_client, eid, cid)
    email, pw = _second_user(demo_client, role="viewer", email="viewer@example.com")
    _login(demo_client, email, pw)
    assert demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"full_name": "X Y"}).status_code == 403
    assert demo_client.post(f"/employers/{eid}/contractors/{cid}/verify-bank", json={"callback_confirmed": True}).status_code == 403
    assert demo_client.post(f"/employers/{eid}/contractors/{cid}/onboarding-link").status_code == 403
    assert demo_client.get("/compliance/overview").status_code == 200  # any member can read
    assert demo_client.get(f"/employers/{eid}/contractors/{cid}/history").status_code == 200
    assert demo_client.get("/compliance/ytd.csv").status_code == 200


def test_tenant_isolation(demo_client):
    eid, cid = _ctx(demo_client)
    _change_bank(demo_client, eid, cid)
    demo_client.post("/auth/logout")
    signup = {"organization": {"name": "Other Org", "country": "gb", "base_currency": "usd", "size_band": "11-50",
                              "industry": "Staffing"},
              "admin": {"full_name": "Olive Other", "email": "olive@other.io", "password": "OtherOrg2026"}}
    assert demo_client.post("/auth/signup", json=signup).status_code == 201
    base = f"/employers/{eid}/contractors/{cid}"
    assert demo_client.patch(base, json={"full_name": "Hacked Name"}).status_code == 404
    assert demo_client.post(f"{base}/verify-bank", json={"callback_confirmed": True}).status_code == 404
    assert demo_client.post(f"{base}/onboarding-link").status_code == 404
    assert demo_client.get(f"{base}/history").status_code == 404
    ov = demo_client.get("/compliance/overview").json()
    assert ov["counts"]["contractors"] == 0 and ov["bank_pending"] == []


# ---------------------------------------------------------------- onboarding
def _link(demo_client, eid, cid):
    r = demo_client.post(f"/employers/{eid}/contractors/{cid}/onboarding-link")
    assert r.status_code == 201, r.text
    return r.json(), r.json()["invite_path"].split("/")[-1]


SEPA_SUBMIT = {"option": "EUR_SEPA", "country": "NL", "iban": "NL91 ABNA 0417 1643 00",
               "tax_status": "NON_US", "confirmed_accurate": True}


def test_onboarding_link_one_time_token_and_submit_stays_pending(demo_client):
    eid, cid = _ctx(demo_client, name="Tomas Rivera")
    inv, token = _link(demo_client, eid, cid)
    assert inv["invite_path"] == f"/onboard/{token}" and len(token) >= 32
    with _db(demo_client) as s:  # only the hash is stored, never the raw token
        row = s.scalar(select(ContractorOnboardingInvite))
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in row.token_hash
        assert (row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)) > timedelta(days=13)
    assert demo_client.get(f"/employers/{eid}/contractors/{cid}/history").json()["invites"][0]["invite_path"] is None

    anon = _anon(demo_client)
    info = anon.get(f"/onboarding/{token}")
    assert info.status_code == 200
    data = info.json()
    assert data["first_name"] == "Tomas" and data["employer_name"] == "Acme Manufacturing GmbH"
    assert data["agency_name"] == "Vertex Payroll Services"
    assert {o["key"] for o in data["payout_options"]} == {"EUR_SWIFT", "EUR_SEPA", "USD_ACH", "GBP_FPS"}
    assert LENA_IBAN not in info.text

    # Unsupported option, missing confirmation, bad IBAN.
    assert anon.post(f"/onboarding/{token}/submit", json={**SEPA_SUBMIT, "option": "JPY_X"}).status_code == 422
    assert anon.post(f"/onboarding/{token}/submit", json={**SEPA_SUBMIT, "confirmed_accurate": False}).status_code == 422
    assert anon.post(f"/onboarding/{token}/submit", json={**SEPA_SUBMIT, "iban": "DE89-3704-0044"}).status_code == 422
    assert anon.post(f"/onboarding/{token}/submit", json={**SEPA_SUBMIT, "tax_id": "123-45-6789"}).status_code == 422

    ok = anon.post(f"/onboarding/{token}/submit", json=SEPA_SUBMIT)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "PENDING_VERIFICATION" and "confirm" in ok.json()["message"]
    assert anon.get(f"/onboarding/{token}").status_code == 404  # used
    assert anon.post(f"/onboarding/{token}/submit", json=SEPA_SUBMIT).status_code == 404

    c = next(x for x in demo_client.get(f"/employers/{eid}").json()["contractors"] if x["id"] == cid)
    assert c["bank_verification_status"] == "PENDING_VERIFICATION"  # the portal never makes details payable
    assert c["iban_last4"] == "4300" and c["onboarded_at"] and c["tax_status"] == "NON_US"
    assert c["tax_form_type"] == "W8BEN" and c["tax_form_status"] == "MISSING"  # form still to be requested
    assert c["bank_change_requested_by_id"] is None
    hist = demo_client.get(f"/employers/{eid}/contractors/{cid}/history").json()
    onboarding = [ch for ch in hist["changes"] if ch["group"] == "onboarding"]
    assert any("Contractor (onboarding link)" == ch["changed_by"] for ch in onboarding)
    assert "NL91ABNA0417164300" not in json.dumps(hist) and "NL91 ABNA" not in json.dumps(hist)
    # Requester unknown (contractor submitted): any permitted staff member may verify.
    v = demo_client.post(f"/employers/{eid}/contractors/{cid}/verify-bank", json={"callback_confirmed": True})
    assert v.status_code == 200, v.text
    assert v.json()["bank_verification_status"] == "VERIFIED"


def test_onboarding_us_ach_rules_and_invalid_provider_check(demo_client):
    eid, cid = _ctx(demo_client, "Cobalt Retail LLC", "Sam Lee")
    _, token = _link(demo_client, eid, cid)
    anon = _anon(demo_client)
    base = {"option": "USD_ACH", "country": "US", "tax_status": "US_PERSON", "confirmed_accurate": True}
    assert anon.post(f"/onboarding/{token}/submit", json={**base, "account_number": "123456789", "routing_code": "12345"}).status_code == 422
    assert anon.post(f"/onboarding/{token}/submit", json={**base, "account_number": "12", "routing_code": "021000021"}).status_code == 422
    ok = anon.post(f"/onboarding/{token}/submit", json={**base, "account_number": "5000 1121", "routing_code": "021000021"})
    assert ok.status_code == 200, ok.text
    c = next(x for x in demo_client.get(f"/employers/{eid}").json()["contractors"] if x["id"] == cid)
    assert c["account_last4"] == "1121" and c["tax_form_status"] == "RECEIVED"  # seeded W-9 kept


def test_onboarding_expired_revoked_and_unknown_look_identical(demo_client):
    eid, cid = _ctx(demo_client)
    inv, token = _link(demo_client, eid, cid)
    anon = _anon(demo_client)
    unknown = anon.get("/onboarding/not-a-real-token")
    # expired
    with _db(demo_client) as s:
        row = s.scalar(select(ContractorOnboardingInvite))
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()
    expired = anon.get(f"/onboarding/{token}")
    assert expired.status_code == 404 and expired.json() == unknown.json()
    assert anon.post(f"/onboarding/{token}/submit", json=SEPA_SUBMIT).status_code == 404
    # revoke path (and a new link revokes the old live one)
    inv2, token2 = _link(demo_client, eid, cid)
    inv3, token3 = _link(demo_client, eid, cid)
    assert anon.get(f"/onboarding/{token2}").status_code == 404
    assert anon.get(f"/onboarding/{token3}").status_code == 200
    r = demo_client.post(f"/employers/{eid}/contractors/{cid}/onboarding-link/{inv3['id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "REVOKED"
    revoked = anon.get(f"/onboarding/{token3}")
    assert revoked.status_code == 404 and revoked.json() == unknown.json()
    assert demo_client.post(f"/employers/{eid}/contractors/{cid}/onboarding-link/{inv3['id']}/revoke").status_code == 404


def test_onboarding_public_endpoints_are_throttled(demo_client):
    anon = _anon(demo_client)
    codes = [anon.get(f"/onboarding/guess-{i}").status_code for i in range(25)]
    assert codes[:20] == [404] * 20 and codes[20:] == [429] * 5


def test_onboarding_link_is_tenant_scoped(demo_client):
    eid, cid = _ctx(demo_client)
    _, token = _link(demo_client, eid, cid)
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Other Org", "country": "gb", "base_currency": "usd", "size_band": "11-50", "industry": "x"},
        "admin": {"full_name": "Olive Other", "email": "olive@other.io", "password": "OtherOrg2026"}})
    # Another org's staff cannot revoke it either.
    assert demo_client.post(f"/employers/{eid}/contractors/{cid}/onboarding-link/{uuid4()}/revoke").status_code == 404


def uuid4():
    import uuid
    return uuid.uuid4()


# ---------------------------------------------------------------- tax form expiry
def test_tax_form_expiry_is_computed_on_read(demo_client):
    eid, cid = _ctx(demo_client, name="Tomas Rivera")
    soon = date.today() + timedelta(days=20)
    r = demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={
        "tax_status": "NON_US", "tax_form_type": "W8BEN", "tax_form_status": "RECEIVED",
        "tax_form_received_at": str(date.today() - timedelta(days=400)), "tax_form_expires_at": str(soon)})
    assert r.status_code == 200
    row = next(x for x in demo_client.get("/compliance/overview").json()["tax_forms"] if x["contractor_id"] == cid)
    assert row["issue"] == "expiring_soon" and row["days_to_expiry"] == 20
    with _db(demo_client) as s:  # stored status still RECEIVED; the date passes
        c = s.get(Contractor, __import__("uuid").UUID(cid))
        c.tax_form_expires_at = date.today() - timedelta(days=1)
        s.commit()
        assert c.tax_form_status == "RECEIVED"
    view = next(x for x in demo_client.get(f"/employers/{eid}").json()["contractors"] if x["id"] == cid)
    assert view["tax_form_status"] == "EXPIRED"
    row = next(x for x in demo_client.get("/compliance/overview").json()["tax_forms"] if x["contractor_id"] == cid)
    assert row["issue"] == "expired"


# ---------------------------------------------------------------- YTD + 1099
def _add_run(client, employer_id, *, lines, when=None, assessment=None, org=None):
    with _db(client) as s:
        emp = s.get(Employer, __import__("uuid").UUID(employer_id))
        run = PayrollRun(organization_id=emp.organization_id, employer_id=emp.id, status="SETTLED", source="manual",
                         assessment=assessment, settled_at=when or datetime.now(UTC), created_at=when or datetime.now(UTC))
        s.add(run)
        s.flush()
        for i, (cid, amount, currency, status) in enumerate(lines):
            s.add(PayLine(run_id=run.id, contractor_id=__import__("uuid").UUID(cid), position=i, beneficiary_name="x",
                          amount=Decimal(amount), currency=currency, method="LOCAL", status=status,
                          request_id=f"r-{run.id}-{i}"))
        s.commit()


def _ytd(client, cid):
    ov = client.get("/compliance/overview").json()
    return next(r for r in ov["ytd"] if r["contractor_id"] == cid), ov


def test_ytd_derivation_and_1099_threshold_edges(demo_client):
    eid, cid = _ctx(demo_client, "Cobalt Retail LLC", "Sam Lee")  # seeded US person with W-9
    # below
    _add_run(demo_client, eid, lines=[(cid, "1999.99", "USD", "PAID")])
    row, ov = _ytd(demo_client, cid)
    assert row["paid_ytd_usd"] == "1999.99" and row["over_1099_threshold"] is False
    assert ov["watch_1099"] == [] and ov["threshold_usd"] == "2000.00"
    # exactly at threshold counts; HELD/FAILED lines never do
    _add_run(demo_client, eid, lines=[(cid, "0.01", "USD", "PAID"), (cid, "500", "USD", "FAILED"), (cid, "500", "USD", "HELD")])
    row, ov = _ytd(demo_client, cid)
    assert row["paid_ytd_usd"] == "2000.00" and row["over_1099_threshold"] is True
    assert [w["contractor_id"] for w in ov["watch_1099"]] == [cid] and ov["watch_1099"][0]["w9_on_file"] is True
    assert ov["counts"]["watch_1099"] == 1
    # a prior-year payment is ignored
    _add_run(demo_client, eid, lines=[(cid, "9000", "USD", "PAID")], when=datetime(date.today().year - 1, 12, 30, tzinfo=UTC))
    row, _ = _ytd(demo_client, cid)
    assert row["paid_ytd_usd"] == "2000.00" and row["payments"] == 2


def test_ytd_uses_stored_run_rate_and_flags_unpriced(demo_client):
    eid, cid = _ctx(demo_client, "Acme Manufacturing GmbH", "Lena Fischer")
    assessment = {"by_currency": [{"currency": "EUR", "payouts": 1, "payroll": "1000.00", "fees": "0.00",
                                   "required": "1000.00", "required_usd": "1100.00"}]}
    _add_run(demo_client, eid, lines=[(cid, "500.00", "EUR", "PAID")], assessment=assessment)
    _add_run(demo_client, eid, lines=[(cid, "100.00", "GBP", "PAID")], assessment=assessment)  # no GBP rate stored
    row, _ = _ytd(demo_client, cid)
    assert row["paid_ytd_usd"] == "550.00"  # 500 EUR * (1100/1000); the GBP line is not guessed
    assert row["unpriced_lines"] == 1 and row["paid_by_currency"] == {"EUR": "500.00", "GBP": "100.00"}


def test_non_us_contractor_never_on_1099_watch(demo_client):
    eid, cid = _ctx(demo_client, "Acme Manufacturing GmbH", "Lena Fischer")  # NON_US
    _add_run(demo_client, eid, lines=[(cid, "5000", "USD", "PAID")])
    row, ov = _ytd(demo_client, cid)
    assert row["paid_ytd_usd"] == "5000.00" and row["over_1099_threshold"] is False and ov["watch_1099"] == []


def test_overview_lists_seeded_states_and_csv_export(demo_client):
    ov = demo_client.get("/compliance/overview").json()
    assert ov["counts"]["bank_pending"] == 1 and ov["bank_pending"][0]["full_name"] == "Sam Lee"
    assert ov["bank_pending"][0]["age_hours"] >= 4
    issues = {r["full_name"]: r["issue"] for r in ov["tax_forms"]}
    assert issues["Tomas Rivera"] == "missing" and issues["Noor van Dijk"] == "expiring_soon"
    assert "Sam Lee" not in issues and "not tax advice" in ov["disclaimer"]
    csv = demo_client.get("/compliance/ytd.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "attachment" in csv.headers["content-disposition"]
    assert "Lena Fischer" in csv.text and "not tax advice" in csv.text
    assert LENA_IBAN not in csv.text and LENA_IBAN not in json.dumps(ov)


def test_csv_neutralises_formula_names(demo_client):
    eid, cid = _ctx(demo_client)
    demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"full_name": "=HYPERLINK(1)"})
    assert "'=HYPERLINK(1)" in demo_client.get("/compliance/ytd.csv").text


# ---------------------------------------------------------------- airwallex validate-only
def test_simulator_validate_beneficiary():
    sim = AirwallexSimulator()
    assert sim.validate_beneficiary(on_behalf_of="x", name="A B", iban=LENA_IBAN, swift_code="COBADEFFXXX") == {"valid": True, "errors": []}
    bad = sim.validate_beneficiary(on_behalf_of="x", name="A B", iban="12", swift_code="COBADEFFXXX")
    assert bad["valid"] is False and "beneficiary.bank_details.iban" in bad["errors"]
    usd = sim.validate_beneficiary(on_behalf_of="x", name="A B", currency="USD", account_number="50001121", routing_code="021000021")
    assert usd["valid"] is True
    assert sim.validate_beneficiary(on_behalf_of="x", name="A B", currency="USD", account_number="50001121", routing_code="1")["valid"] is False


def _live(handler):
    settings = Settings(app_env="live", airwallex_client_id="id", airwallex_api_key="key")

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json={"token": "t", "expires_at": "2099-01-01T00:00:00+0000"})
        return handler(request)

    return AirwallexClient(settings, transport=httpx.MockTransport(route))


def test_client_validate_only_calls_validate_never_create():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={})

    out = _live(handler).validate_beneficiary(on_behalf_of="acct_x", name="A B", iban=LENA_IBAN, swift_code="COBADEFFXXX")
    assert out == {"valid": True, "errors": []}
    api = [p for p in seen if "/authentication" not in p]
    assert api == ["/api/v1/beneficiaries/validate"]


def test_client_validate_returns_field_names_only():
    def handler(req):
        return httpx.Response(400, json={"code": "validation_failed", "message": f"bad {LENA_IBAN}",
                                         "details": {"errors": [{"source": "beneficiary.bank_details.iban", "code": "invalid"}]}})

    out = _live(handler).validate_beneficiary(on_behalf_of="acct_x", name="A B", iban=LENA_IBAN)
    assert out == {"valid": False, "errors": ["beneficiary.bank_details.iban"]}
    assert LENA_IBAN not in json.dumps(out)


# ---------------------------------------------------------------- migration
def test_migration_adds_trust_columns_with_safe_defaults(tmp_path):
    path = tmp_path / "old_contractors.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contractors (
            id CHAR(32) PRIMARY KEY, organization_id CHAR(32) NOT NULL, employer_id CHAR(32) NOT NULL,
            full_name VARCHAR(120) NOT NULL, email VARCHAR(254), country VARCHAR(2) NOT NULL,
            currency VARCHAR(3) NOT NULL, payout_method VARCHAR(8) NOT NULL, iban VARCHAR(34),
            swift_code VARCHAR(11), account_number VARCHAR(34), routing_code VARCHAR(20), default_amount NUMERIC(19,2),
            airwallex_beneficiary_id VARCHAR(64), is_active BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
        INSERT INTO contractors VALUES ('a0000000000000000000000000000000', 'b0000000000000000000000000000000',
            'c0000000000000000000000000000000', 'Old Row', NULL, 'DE', 'EUR', 'SWIFT', 'DE89370400440532013000',
            'COBADEFFXXX', NULL, NULL, NULL, NULL, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
    """)
    conn.commit()
    conn.close()
    db = Database.from_url(f"sqlite:///{path.as_posix()}")
    db.create_schema()
    cols = {c["name"]: c for c in inspect(db.engine).get_columns("contractors")}
    for name in ("tax_status", "tax_form_type", "tax_form_status", "tax_form_received_at", "tax_form_expires_at",
                 "bank_verification_status", "bank_details_changed_at", "bank_change_requested_by_id",
                 "bank_verified_by_id", "bank_verified_at", "onboarded_at"):
        assert name in cols
    assert {"contractor_changes", "contractor_onboarding_invites"} <= set(inspect(db.engine).get_table_names())
    with db.sessionmaker() as s:  # existing rows stay payable and untracked-for-tax
        row = s.scalar(select(Contractor))
        assert row.bank_verification_status == "VERIFIED" and row.tax_status == "UNKNOWN"
        assert row.tax_form_type == "NONE" and row.tax_form_status == "MISSING"
    db.create_schema()  # idempotent


def test_seed_is_idempotent_and_keeps_edits(demo_client):
    eid, cid = _ctx(demo_client, name="Tomas Rivera")
    demo_client.patch(f"/employers/{eid}/contractors/{cid}", json={"tax_status": "US_PERSON"})
    from app.services.seed import seed_demo
    runtime = demo_client.app.state.runtime
    with _db(demo_client) as s:
        seed_demo(s, runtime)
    c = next(x for x in demo_client.get(f"/employers/{eid}").json()["contractors"] if x["id"] == cid)
    assert c["tax_status"] == "US_PERSON"
    with _db(demo_client) as s:
        assert len(s.scalars(select(ContractorChange).where(ContractorChange.change_group == "bank")).all()) == 1


def test_bank_check_preview_is_read_only_and_gated(demo_client):
    eid, cid = _ctx(demo_client)
    assert demo_client.get(f"/employers/{eid}/contractors/{cid}/bank-check").json() == {"valid": True, "checked": True, "errors": []}
    email, pw = _second_user(demo_client, role="viewer", email="v2@example.com")
    _login(demo_client, email, pw)
    assert demo_client.get(f"/employers/{eid}/contractors/{cid}/bank-check").status_code == 403
