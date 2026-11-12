"""Live Airi VIC adapter + two-phase benefits flow, against a FAKE airi-cli.

The fake (``tests/fixtures/fake_airi_cli.py``) emits the JSON shapes of the
real airi-cli. Nothing here talks to Airi. Card data in the fake credential
file (the published test PAN) must never reach the DB, API responses or logs.
"""

from __future__ import annotations

import json
import shlex
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.db import Database
from app.partners.vic import (
    AiriVicAdapter,
    CheckoutOutcome,
    HumanConfirmedCheckoutExecutor,
    SimulatedVicAdapter,
    VicError,
    VicUnavailable,
    build_vic_adapter,
    merchant_url_for,
    vic_status,
)
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

FAKE = Path(__file__).parent / "fixtures" / "fake_airi_cli.py"
PAN = "4111111111111111"


def _command() -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"


def _settings(**kw) -> Settings:
    return Settings(database_url="sqlite://", jwt_secret="test-secret-" + "x" * 32, seed_demo=True,
                    vic_mode="live", airi_cli_command=_command(), **kw)


@pytest.fixture
def fake_dir(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_DIR", str(d))
    for k in ("FAKE_AUTH", "FAKE_PM", "FAKE_CRED", "FAKE_RESULT_FAIL"):
        monkeypatch.delenv(k, raising=False)
    return d


def _calls(d: Path) -> list[list[str]]:
    p = d / "calls.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _adapter() -> AiriVicAdapter:
    return AiriVicAdapter(_settings())


def _mandate(adapter, amount="42.00", merchant="coursera.org"):
    return adapter.create_mandate(merchant=merchant, merchant_url="https://coursera.org",
                                  amount=Decimal(amount), currency="USD", item="Course: Python, 101",
                                  description="PaySilo benefit")


# --------------------------------------------------------------------------- #
# adapter unit tests
# --------------------------------------------------------------------------- #

def test_build_and_status_modes(fake_dir, monkeypatch):
    assert isinstance(build_vic_adapter(Settings(database_url="sqlite://")), SimulatedVicAdapter)
    s = _settings()
    a = build_vic_adapter(s)
    assert isinstance(a, AiriVicAdapter)
    st = vic_status(s, a)
    assert st["mode"] == "VIC" and st["cli_available"] and st["authenticated"]
    monkeypatch.setenv("FAKE_AUTH", "out")
    a2 = AiriVicAdapter(s)
    st = vic_status(s, a2)
    assert st["mode"] == "VIC_UNAVAILABLE" and st["cli_available"] and not st["authenticated"]
    missing = Settings(database_url="sqlite://", vic_mode="live", airi_cli_command="/nonexistent/airi-cli")
    st = vic_status(missing, build_vic_adapter(missing))
    assert st["mode"] == "VIC_UNAVAILABLE" and not st["cli_available"]
    sim = vic_status(Settings(database_url="sqlite://"), SimulatedVicAdapter())
    assert sim["mode"] == "SIMULATED"


def test_create_mandate_uses_real_cli_shape(fake_dir):
    info = _mandate(_adapter())
    assert info.mandate_id == "mdt_fake_1"
    assert info.approval_url == "https://app.airi.example/passkey/mdt_fake_1"
    assert info.payment_method_ref == "airi_pm_pm-fake-"
    argv = json.loads((fake_dir / "create.json").read_text())
    flags = dict(zip(argv[2::2], argv[3::2]))
    assert flags["--source"] == "paysilo"
    assert flags["--payment-method-id"] == "pm-fake-0001-aaaa"
    assert flags["--total-amount"] == "42.00" and flags["--currency"] == "USD"
    assert flags["--merchant-country"] == "US"
    # commas/colons in the item name must not break the name:..,unit_price:.. syntax
    assert flags["--item"] == "name:Course  Python  101,unit_price:42.00,quantity:1"


def test_cap_enforced_before_any_cli_call(fake_dir):
    with pytest.raises(VicError) as e:
        _mandate(_adapter(), amount="200.01")
    assert e.value.code == "TX_LIMIT_EXCEEDED"
    assert _calls(fake_dir) == []  # the CLI was never invoked


def test_missing_binary_and_not_signed_in_fail_closed(fake_dir, monkeypatch):
    missing = AiriVicAdapter(Settings(database_url="sqlite://", vic_mode="live",
                                      airi_cli_command="/nonexistent/airi-cli"))
    with pytest.raises(VicUnavailable):
        _mandate(missing)
    monkeypatch.setenv("FAKE_AUTH", "out")
    with pytest.raises(VicUnavailable, match="not signed in"):
        _mandate(_adapter())
    assert not any(c[:2] == ["mandate", "create"] for c in _calls(fake_dir))


def test_no_payment_method(fake_dir, monkeypatch):
    monkeypatch.setenv("FAKE_PM", "empty")
    with pytest.raises(VicError) as e:
        _mandate(_adapter())
    assert e.value.code == "NO_PAYMENT_METHOD"


def test_approval_pending_then_credential_then_result(fake_dir, monkeypatch, caplog):
    a = _adapter()
    info = _mandate(a)
    ex = HumanConfirmedCheckoutExecutor(True)

    monkeypatch.setenv("FAKE_CRED", "pending")  # human has not opened the passkey URL yet
    res = a.complete(mandate_id=info.mandate_id, merchant="coursera.org", amount=Decimal("42"),
                     currency="USD", executor=ex)
    assert res.state == "AWAITING_USER_AUTHORIZATION" and not res.ok
    assert not (fake_dir / "result.json").exists()  # nothing to report yet

    monkeypatch.delenv("FAKE_CRED")
    with caplog.at_level("DEBUG"):
        res = a.complete(mandate_id=info.mandate_id, merchant="coursera.org", amount=Decimal("42"),
                         currency="USD", executor=ex)
    assert res.ok and res.state == "COMPLETED" and res.result_reported
    assert res.receipt == {"status": "READY", "tokenType": "NETWORK_TOKEN",
                           "tokenExpiration": "2026-11-12T20:00:00+05:30"}
    argv = json.loads((fake_dir / "result.json").read_text())
    assert argv[-3:-1] == ["--result", "SUCCEEDED"] or "SUCCEEDED" in argv
    # the credential file existed during the call and is gone afterwards
    cred = Path((fake_dir / "credpath.txt").read_text())
    assert not cred.exists() and not cred.parent.exists()
    # no card data in anything we hold or logged
    assert PAN not in repr(res) and PAN not in caplog.text


def test_unconfirmed_purchase_reports_cancelled(fake_dir):
    a = _adapter()
    info = _mandate(a)
    res = a.complete(mandate_id=info.mandate_id, merchant="m.example", amount=Decimal("5"),
                     currency="USD", executor=HumanConfirmedCheckoutExecutor(False))
    assert not res.ok and res.state == "CANCELLED" and res.failure_code == "PURCHASE_NOT_CONFIRMED"
    argv = json.loads((fake_dir / "result.json").read_text())
    assert "CANCELLED" in argv


def test_declined_is_reported_with_reason(fake_dir):
    class Declining:
        def execute(self, **kw):
            return CheckoutOutcome("DECLINED", "merchant said no")

    a = _adapter()
    info = _mandate(a)
    res = a.complete(mandate_id=info.mandate_id, merchant="m.example", amount=Decimal("5"),
                     currency="USD", executor=Declining())
    assert res.state == "DECLINED" and res.failure_code == "PROVIDER_DECLINED"
    argv = json.loads((fake_dir / "result.json").read_text())
    assert "DECLINED" in argv and argv[argv.index("--reason") + 1] == "merchant said no"


def test_executor_crash_still_reports_and_deletes_file(fake_dir):
    class Boom:
        def execute(self, **kw):
            raise RuntimeError("checkout exploded")

    a = _adapter()
    info = _mandate(a)
    res = a.complete(mandate_id=info.mandate_id, merchant="m.example", amount=Decimal("5"),
                     currency="USD", executor=Boom())
    assert res.state == "CANCELLED"
    assert "CANCELLED" in json.loads((fake_dir / "result.json").read_text())
    assert not Path((fake_dir / "credpath.txt").read_text()).exists()


def test_credential_error_is_failed_not_success(fake_dir, monkeypatch):
    a = _adapter()
    info = _mandate(a)
    monkeypatch.setenv("FAKE_CRED", "error")
    res = a.complete(mandate_id=info.mandate_id, merchant="m.example", amount=Decimal("5"),
                     currency="USD", executor=HumanConfirmedCheckoutExecutor(True))
    assert res.state == "FAILED" and not res.ok and "denied" in res.failure_detail


def test_result_report_failure_is_surfaced(fake_dir, monkeypatch):
    monkeypatch.setenv("FAKE_RESULT_FAIL", "1")
    a = _adapter()
    info = _mandate(a)
    res = a.complete(mandate_id=info.mandate_id, merchant="m.example", amount=Decimal("5"),
                     currency="USD", executor=HumanConfirmedCheckoutExecutor(True))
    assert res.ok and res.result_reported is False


def test_cancel_mandate(fake_dir):
    a = _adapter()
    info = _mandate(a)
    a.cancel_mandate(info.mandate_id)
    assert info.mandate_id in json.loads((fake_dir / "cancel.json").read_text())


def test_adapter_timeout_means_still_pending(fake_dir, monkeypatch):
    slow = tmp = fake_dir.parent / "slow.py"
    slow.write_text("import time; time.sleep(30)\n")
    a = AiriVicAdapter(Settings(database_url="sqlite://", vic_mode="live",
                                airi_cli_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(tmp))}"))
    # whoami must succeed for ensure_ready; fake it as ready, then time out get-credential.
    a._probe_val, a._probe_at = (True, True), 1e18
    a.ensure_ready = lambda: None
    a._GRACE = 1
    res = a.complete(mandate_id="mdt_x", merchant="m.example", amount=Decimal("5"), currency="USD",
                     executor=HumanConfirmedCheckoutExecutor(True), wait_seconds=0)
    assert res.state == "AWAITING_USER_AUTHORIZATION"


def test_merchant_url_never_invented():
    assert merchant_url_for("coursera.org") == "https://coursera.org"
    assert merchant_url_for("https://shop.example/x") == "https://shop.example/x"
    assert merchant_url_for("O'Reilly Media") is None


def test_wsl_template_command_and_path(monkeypatch):
    a = AiriVicAdapter(Settings(database_url="sqlite://", vic_mode="live",
                                airi_cli_command="wsl -d Ubuntu-24.04 -- bash -lc 'airi-cli {args}'"))
    assert a._argv(["auth", "whoami", "-o", "json"]) == [
        "wsl", "-d", "Ubuntu-24.04", "--", "bash", "-lc", "airi-cli auth whoami -o json"]
    path, cleanup = a._credential_path()
    assert path.startswith("/tmp/paysilo-cred-")
    calls = []
    monkeypatch.setattr("app.partners.vic.subprocess.run", lambda argv, **kw: calls.append(argv))
    cleanup()
    assert calls == [["wsl", "-d", "Ubuntu-24.04", "--", "rm", "-f", path]]


# --------------------------------------------------------------------------- #
# two-phase through the API
# --------------------------------------------------------------------------- #

@pytest.fixture
def live_client(fake_dir):
    app = create_app(_settings(), Database.from_url("sqlite://"))
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        assert r.status_code == 200, r.text
        yield c


def _new_approved(c, merchant="coursera.org", amount="42.00"):
    emp = next(e for e in c.get("/employers").json() if e["name"].startswith("Acme"))
    detail = c.get(f"/employers/{emp['id']}").json()
    contractor = detail["contractors"][0]
    r = c.post("/benefits/requests", json={
        "employer_id": emp["id"], "merchant": merchant, "item": "Python course",
        "amount": amount, "currency": "USD", "contractor_id": contractor["id"],
        "contractor_name": contractor["full_name"]})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = c.post(f"/benefits/requests/{rid}/decision", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    return rid


def test_live_seed_creates_no_mandate_or_fake_purchase(live_client, fake_dir):
    assert not any(c[:2] == ["mandate", "create"] for c in _calls(fake_dir))
    assert all(p["status"] != "SUCCEEDED" for p in live_client.get("/benefits/purchases").json())


def test_two_phase_flow_end_to_end(live_client, fake_dir, monkeypatch, caplog):
    c = live_client
    st = c.get("/benefits/status").json()
    assert st["mode"] == "VIC" and st["two_phase"] is True
    rid = _new_approved(c)

    # phase A: mandate + approval URL, nothing spent
    r = c.post(f"/benefits/requests/{rid}/purchase", json={})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "PENDING" and p["mode"] == "VIC"
    assert p["mandate_status"] == "AWAITING_USER_AUTHORIZATION"
    assert p["provider_mandate_id"] == "mdt_fake_1"
    assert p["approval_url"].startswith("https://app.airi.example/passkey/")
    assert p["provider_ref"] is None and p["journal_txn_id"] is None

    # idempotent: a retry reuses the same mandate (one mandate per purchase)
    again = c.post(f"/benefits/requests/{rid}/purchase", json={}).json()
    assert again["id"] == p["id"] and again["provider_mandate_id"] == "mdt_fake_1"
    assert sum(1 for x in _calls(fake_dir) if x[:2] == ["mandate", "create"]) == 1

    # human has not authorized yet -> still pending
    monkeypatch.setenv("FAKE_CRED", "pending")
    r = c.post(f"/benefits/purchases/{p['id']}/complete", json={"purchase_confirmed": True})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"

    # authorized + human confirms the purchase -> SUCCEEDED
    monkeypatch.delenv("FAKE_CRED")
    with caplog.at_level("DEBUG"):
        r = c.post(f"/benefits/purchases/{p['id']}/complete", json={"purchase_confirmed": True})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "SUCCEEDED" and done["mandate_status"] == "COMPLETED"
    assert done["journal_txn_id"] is not None
    assert PAN not in r.text and PAN not in caplog.text
    reqs = {x["id"]: x for x in c.get("/benefits/requests").json()}
    assert reqs[rid]["status"] == "PURCHASED"
    # idempotent completion
    assert c.post(f"/benefits/purchases/{p['id']}/complete", json={}).json()["status"] == "SUCCEEDED"


def test_complete_without_confirmation_is_cancelled(live_client, fake_dir):
    c = live_client
    rid = _new_approved(c)
    p = c.post(f"/benefits/requests/{rid}/purchase", json={}).json()
    done = c.post(f"/benefits/purchases/{p['id']}/complete", json={"purchase_confirmed": False}).json()
    assert done["status"] == "FAILED" and done["failure_code"] == "PURCHASE_NOT_CONFIRMED"
    assert done["mandate_status"] == "CANCELLED" and done["journal_txn_id"] is None
    assert "CANCELLED" in json.loads((fake_dir / "result.json").read_text())


def test_cancel_pending_purchase(live_client, fake_dir):
    c = live_client
    rid = _new_approved(c)
    p = c.post(f"/benefits/requests/{rid}/purchase", json={}).json()
    done = c.post(f"/benefits/purchases/{p['id']}/cancel").json()
    assert done["status"] == "FAILED" and done["mandate_status"] == "CANCELLED"
    assert (fake_dir / "cancel.json").exists()
    # a retry now makes a NEW mandate (new attempt)
    p2 = c.post(f"/benefits/requests/{rid}/purchase", json={}).json()
    assert p2["id"] != p["id"] and p2["provider_mandate_id"] == "mdt_fake_2"


def test_live_cap_and_unavailable_checked_in_code_first(live_client, fake_dir, monkeypatch):
    c = live_client
    rid = _new_approved(c, amount="250.00")
    r = c.post(f"/benefits/requests/{rid}/purchase", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TX_LIMIT_EXCEEDED"
    assert not any(x[:2] == ["mandate", "create"] for x in _calls(fake_dir))

    rid2 = _new_approved(c)
    monkeypatch.setenv("FAKE_AUTH", "out")
    r = c.post(f"/benefits/requests/{rid2}/purchase", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "VIC_UNAVAILABLE"
    assert c.get("/benefits/status").json()["mode"] in ("VIC", "VIC_UNAVAILABLE")


def test_live_requires_merchant_website(live_client):
    rid = _new_approved(live_client, merchant="Some Bookshop")
    r = live_client.post(f"/benefits/requests/{rid}/purchase", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MERCHANT_URL_REQUIRED"


def test_foreign_org_purchase_is_404(live_client):
    import uuid
    r = live_client.post(f"/benefits/purchases/{uuid.uuid4()}/complete", json={})
    assert r.status_code == 404
    assert live_client.post(f"/benefits/purchases/{uuid.uuid4()}/cancel").status_code == 404
