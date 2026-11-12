"""Connect Airi: adapter login/poll/logout and the owner/admin routes, against the FAKE airi-cli."""

from __future__ import annotations

import json
import logging
import shlex
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.db import Database
from app.partners.vic import AiriVicAdapter, VicError, VicUnavailable, validate_verification_url
from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD
from tests.test_vic_airi import _settings

CODE_PREFIX = "WXYZ-"
SECRET = "test-secret-" + "x" * 32


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    monkeypatch.setenv("FAKE_DIR", str(d))
    for k in ("FAKE_AUTH", "FAKE_PM", "FAKE_CRED", "FAKE_RESULT_FAIL", "FAKE_LOGIN", "FAKE_POLL_PENDING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_AUTH", "out")  # start signed out
    return d


def _calls(d: Path) -> list[list[str]]:
    p = d / "calls.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _n(d, *prefix):
    return len([c for c in _calls(d) if c[: len(prefix)] == list(prefix)])


# ------------------------------------------------------------------ adapter

def test_login_then_poll_connects_and_reads_only_expiry(fake):
    a = AiriVicAdapter(_settings())
    assert a.auth_status()["authenticated"] is False
    r = a.start_login()
    assert r["state"] == "pending" and r["user_code"].startswith(CODE_PREFIX)
    assert r["verification_uri"] == "https://app.airi.com/device" and 0 < r["expires_in"] <= 600
    login = next(c for c in _calls(fake) if c[:2] == ["auth", "login"])
    assert login[login.index("--client-name") + 1] == "PaySilo"
    done = a.poll_login()
    assert done == {"state": "connected", "session_expires": "2026-12-01T00:00:00Z"}
    assert a.auth_status()["authenticated"] is True
    assert "user@example.test" not in repr(vars(a)) and "5550100" not in repr(vars(a))


def test_pending_login_is_reused_not_recreated(fake, monkeypatch):
    monkeypatch.setenv("FAKE_POLL_PENDING", "2")
    a = AiriVicAdapter(_settings())
    first = a.start_login()
    second = a.start_login()
    assert second["user_code"] == first["user_code"] and _n(fake, "auth", "login") == 1
    assert a.poll_login()["state"] == "pending"
    assert a.poll_login()["state"] == "pending"
    assert a.start_login()["user_code"] == first["user_code"]
    assert a.poll_login()["state"] == "connected"
    assert _n(fake, "auth", "login") == 1


def test_already_authenticated_never_logs_in(fake, monkeypatch):
    monkeypatch.delenv("FAKE_AUTH")
    a = AiriVicAdapter(_settings())
    assert a.start_login()["state"] == "connected" and _n(fake, "auth", "login") == 0


@pytest.mark.parametrize("scenario,state", [("denied", "denied"), ("expired", "expired")])
def test_denied_or_expired_clears_state(fake, monkeypatch, scenario, state):
    monkeypatch.setenv("FAKE_LOGIN", scenario)
    a = AiriVicAdapter(_settings())
    a.start_login()
    r = a.poll_login()
    assert r["state"] == state and r["message"]
    assert a.auth_status()["pending"] is False
    monkeypatch.setenv("FAKE_LOGIN", "approve")
    assert a.start_login()["user_code"].startswith(CODE_PREFIX)  # a fresh code is allowed again
    assert _n(fake, "auth", "login") == 2


def test_poll_is_bounded_by_short_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN", "slow")
    a = AiriVicAdapter(_settings())
    a.start_login()
    t = time.monotonic()
    assert a.poll_login(timeout_s=1.0)["state"] == "pending"
    assert time.monotonic() - t < 4


def test_local_expiry_drops_pending(fake):
    a = AiriVicAdapter(_settings())
    a.start_login()
    a._pending["expires_at"] -= 10_000
    assert a.poll_login()["state"] == "expired"


def test_garbage_and_failure_and_missing_cli(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN", "garbage")
    with pytest.raises(VicError):
        AiriVicAdapter(_settings()).start_login()
    monkeypatch.setenv("FAKE_LOGIN", "noconn")
    with pytest.raises(VicError):
        AiriVicAdapter(_settings()).start_login()
    missing = AiriVicAdapter(Settings(database_url="sqlite://", vic_mode="live",
                                      airi_cli_command="/nonexistent/airi-cli"))
    with pytest.raises(VicUnavailable):
        missing.start_login()
    assert missing.auth_status()["cli_available"] is False


def test_off_domain_url_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN", "evil")
    a = AiriVicAdapter(_settings())
    with pytest.raises(VicError, match="not on an Airi"):
        a.start_login()
    assert a.auth_status()["pending"] is False


@pytest.mark.parametrize("url,ok", [
    ("https://app.airi.com/x", True), ("https://airi.com", True), ("https://login.airwallex.com/d?c=1", True),
    ("http://app.airi.com/x", False), ("https://airi.com.evil.example/x", False),
    ("https://evilairi.com/x", False), ("https://user@airi.com/x", False),
    ("javascript:alert(1)", False), ("https://airi.com:8443/x", False), ("", False)])
def test_validate_verification_url(url, ok):
    if ok:
        assert validate_verification_url(url) == url
    else:
        with pytest.raises(VicError):
            validate_verification_url(url)


def test_logout(fake, monkeypatch):
    monkeypatch.delenv("FAKE_AUTH")
    a = AiriVicAdapter(_settings())
    assert a.auth_status()["authenticated"] is True
    assert a.logout() == {"state": "disconnected"}
    assert a.auth_status()["authenticated"] is False


def test_tolerant_field_names(fake, tmp_path):
    alt = tmp_path / "alt.py"
    alt.write_text("import json, sys\n"
                   "if sys.argv[1:3] == ['auth', 'whoami']:\n"
                   "    print(json.dumps({'authenticated': False}))\n"
                   "else:\n"
                   "    print(json.dumps({'verification_url': 'https://app.airwallex.com/d', 'code': 'AB-12'}))\n")
    a = AiriVicAdapter(Settings(database_url="sqlite://", vic_mode="live",
                                airi_cli_command=f"{shlex.quote(sys.executable)} {shlex.quote(str(alt))}"))
    r = a.start_login()
    assert (r["verification_uri"], r["user_code"]) == ("https://app.airwallex.com/d", "AB-12")


# ------------------------------------------------------------------ routes

def _client(settings):
    return TestClient(create_app(settings, Database.from_url("sqlite://")))


@pytest.fixture
def client(fake):
    with _client(_settings()) as c:
        assert c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}).status_code == 200
        yield c


def test_full_route_flow_and_audit_has_no_code(client, fake, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FAKE_POLL_PENDING", "1")
    assert client.get("/benefits/status").json()["mode"] == "VIC_UNAVAILABLE"
    assert client.get("/benefits/airi/status").json()["authenticated"] is False
    r = client.post("/benefits/airi/connect")
    assert r.status_code == 200 and r.json()["state"] == "pending"
    code = r.json()["user_code"]
    assert client.post("/benefits/airi/connect").json()["user_code"] == code
    assert client.post("/benefits/airi/poll").json()["state"] == "pending"
    done = client.post("/benefits/airi/poll").json()
    assert done["state"] == "connected" and done["session_expires"] == "2026-12-01T00:00:00Z"
    st = client.get("/benefits/status").json()
    assert st["mode"] == "VIC" and st["session_expires"] == "2026-12-01T00:00:00Z"
    assert client.post("/benefits/airi/disconnect").json()["state"] == "disconnected"
    assert client.get("/benefits/status").json()["mode"] == "VIC_UNAVAILABLE"
    audits = [rec.getMessage() for rec in caplog.records if rec.name == "paysilo.audit"]
    assert any("airi.connect.connected" in m for m in audits) and any("airi.disconnected" in m for m in audits)
    assert all("user=" in m for m in audits)
    assert code not in caplog.text and "app.airi.com" not in caplog.text


def test_simulated_mode_is_409(fake):
    with _client(Settings(database_url="sqlite://", jwt_secret=SECRET, seed_demo=True)) as c:
        c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        for method, path in (("get", "status"), ("post", "connect"), ("post", "poll"), ("post", "disconnect")):
            r = getattr(c, method)(f"/benefits/airi/{path}")
            assert r.status_code == 409 and "VIC_MODE is simulated" in r.json()["detail"]["message"]


def test_requires_session_and_owner_admin(client, fake):
    inv = client.post("/org/invitations", json={"email": "viewer.airi@example.com", "role": "viewer"}).json()
    appr = client.post("/org/invitations", json={"email": "appr.airi@example.com", "role": "approver"}).json()
    client.post("/auth/logout")
    for path in ("status", "connect", "poll", "disconnect"):
        m = client.get if path == "status" else client.post
        assert m(f"/benefits/airi/{path}").status_code == 401
    for invite, email in ((inv, "viewer.airi@example.com"), (appr, "appr.airi@example.com")):
        token = invite["invite_path"].split("/")[-1]
        assert client.post(f"/invitations/{token}/accept",
                           json={"full_name": "Some One", "password": "SomePass2026x"}).status_code == 200
        assert client.post("/auth/login", json={"email": email, "password": "SomePass2026x"}).status_code == 200
        assert client.get("/benefits/airi/status").status_code == 403
        for path in ("connect", "poll", "disconnect"):
            assert client.post(f"/benefits/airi/{path}").status_code == 403
        client.post("/auth/logout")
    assert _n(fake, "auth", "login") == 0 and _n(fake, "auth", "logout") == 0


def test_off_domain_url_never_reaches_browser(client, fake, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN", "evil")
    r = client.post("/benefits/airi/connect")
    assert r.status_code == 502 and "evil.example" not in r.text


def test_connect_rate_limited(client, fake):
    assert [client.post("/benefits/airi/connect").status_code for _ in range(5)] == [200] * 5
    r = client.post("/benefits/airi/connect")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_denied_route_message(client, fake, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN", "denied")
    client.post("/benefits/airi/connect")
    r = client.post("/benefits/airi/poll").json()
    assert r["state"] == "denied" and "denied" in r["message"].lower()


def test_cli_missing_is_409(fake):
    s = Settings(database_url="sqlite://", jwt_secret=SECRET, seed_demo=True,
                 vic_mode="live", airi_cli_command="/nonexistent/airi-cli")
    with _client(s) as c:
        c.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        r = c.post("/benefits/airi/connect")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "AIRI_CLI_UNAVAILABLE"
