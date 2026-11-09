import base64
import time
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.partners.tap import (
    TAP_ALGORITHM,
    TapAgentRegistry,
    TapReplayGuard,
    derive_key_id,
    generate_tap_agent,
    load_tap_agent,
    sign_request,
    verify_request,
)
from app.partners.tap_signing import (
    TapSigner,
    registry_from_tap_key_rows,
    signer_from_key_material,
    verify_stored_approval,
)

AUTHORITY = "api-demo.airwallex.com"
PATH = "/api/v1/transfers/create"
BODY = '{"employer":"Acme Manufacturing GmbH","amount":"3400.00","currency":"EUR"}'


def _private_pem(private: Ed25519PrivateKey) -> str:
    return private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")


def _raw_seed_b64(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())).decode("ascii")


def _setup():
    key = generate_tap_agent("paysilo-agent")
    registry = TapAgentRegistry()
    registry.register(key)
    return key, registry


def test_sign_verify_round_trip():
    key, registry = _setup()
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY)
    result = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=registry, replay_guard=TapReplayGuard(), body=BODY)
    assert result.valid is True
    assert result.agent_id == "paysilo-agent"


def test_tampered_body_rejected():
    key, registry = _setup()
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY)
    tampered = BODY.replace("3400.00", "9999.00")
    result = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=registry, replay_guard=TapReplayGuard(), body=tampered)
    assert result.valid is False
    assert result.code == "digest_mismatch"


def test_replayed_nonce_rejected():
    key, registry = _setup()
    guard = TapReplayGuard()
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY)
    first = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                           registry=registry, replay_guard=guard, body=BODY)
    second = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=registry, replay_guard=guard, body=BODY)
    assert first.valid is True
    assert second.valid is False
    assert second.code == "replayed"


def test_expired_signature_rejected():
    key, registry = _setup()
    now = int(time.time())
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY,
                           created=now - 1000, expires=now - 900)
    result = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=registry, body=BODY)
    assert result.valid is False
    assert result.code == "expired"


def test_unregistered_agent_rejected():
    key, _ = _setup()
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY)
    result = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=TapAgentRegistry(), body=BODY)
    assert result.valid is False
    assert result.code == "unregistered_agent"


def test_covered_digest_without_body_fails_closed():
    key, registry = _setup()
    headers = sign_request(key, method="POST", authority=AUTHORITY, path=PATH, body=BODY)
    result = verify_request(method="POST", authority=AUTHORITY, path=PATH, headers=headers,
                            registry=registry, body=None)
    assert result.valid is False
    assert result.code == "missing_body"


# ---------------------------------------------------------------------------
# Persistent keys from TAP_PRIVATE_KEY (Issue #8)
# ---------------------------------------------------------------------------

def test_load_tap_agent_accepts_pem_base64_pem_and_raw_seed():
    private = Ed25519PrivateKey.generate()
    pem = _private_pem(private)
    for material in (pem, base64.b64encode(pem.encode()).decode(), _raw_seed_b64(private)):
        key = load_tap_agent("paysilo-agent", material)
        assert key.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) == \
            private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_load_tap_agent_rejects_garbage():
    with pytest.raises(ValueError):
        load_tap_agent("paysilo-agent", "not-a-key!!")
    with pytest.raises(ValueError):
        load_tap_agent("paysilo-agent", "   ")


def test_env_key_restart_persistence_same_keyid_and_verifies():
    """Simulated restart: a fresh signer/registry built from the same
    TAP_PRIVATE_KEY material must verify signatures made before the restart."""
    private = Ed25519PrivateKey.generate()
    material = base64.b64encode(_private_pem(private).encode()).decode()

    before = signer_from_key_material("paysilo-agent", material)
    envelope = before.sign_approval(
        run_id="run-1", employer_key="emp_acme", decision="approve",
        assessment={"required_usd": "6754.88", "total_required": "6025.70", "payroll_currency": "EUR"},
        pay_lines=[{"name": "Lena Fischer", "amount": "3400.00", "currency": "EUR", "method": "SWIFT"}],
        decided_by="ops@example.com")

    after = signer_from_key_material("paysilo-agent", material)  # "restarted" process state
    assert after.key.key_id == before.key.key_id
    assert after.key.key_id == derive_key_id("paysilo-agent", private.public_key())
    result = after.verify(envelope, {
        "type": "payroll.approval", "run_id": "run-1", "employer": "emp_acme", "decision": "approve",
        "decided_by": "ops@example.com", "required_usd": "6754.88", "total_required": "6025.70",
        "payroll_currency": "EUR",
        "pay_lines": [{"name": "Lena Fischer", "amount": "3400.00", "currency": "EUR", "method": "SWIFT"}]})
    assert result.valid is True and result.key_id == after.key.key_id


def test_settings_read_tap_private_key_from_env(monkeypatch):
    monkeypatch.setenv("TAP_PRIVATE_KEY", _raw_seed_b64(Ed25519PrivateKey.generate()))
    from app.config import Settings

    settings = Settings()
    assert settings.tap_private_key is not None
    key = load_tap_agent(settings.tap_agent_id, settings.tap_private_key,
                         key_id=settings.tap_key_id)
    assert key.key_id.startswith("paysilo-agent-")


# ---------------------------------------------------------------------------
# Key rotation (Issue #8)
# ---------------------------------------------------------------------------

def test_key_rotation_old_signatures_still_verify():
    class Row:  # minimal tap_keys row stand-in
        def __init__(self, key):
            self.key_id, self.agent_id, self.public_key_pem = key.key_id, key.agent_id, key.public_key_pem()

    old_signer = TapSigner(key=generate_tap_agent("paysilo-agent", key_id="paysilo-agent-old"))
    envelope = old_signer.sign_payout(
        run_id="run-9", employer_key="emp_acme", account_id="acct_1", index=0,
        line={"name": "Lena Fischer", "amount": "3400.00", "currency": "EUR", "method": "SWIFT"})

    # Rotation: a new active key signs from now on; the registry is rebuilt
    # from the tap_keys table, which still holds the retired key.
    new_signer = TapSigner(key=generate_tap_agent("paysilo-agent", key_id="paysilo-agent-new"))
    registry = registry_from_tap_key_rows([Row(old_signer.key), Row(new_signer.key)])
    assert registry.key_ids() == ["paysilo-agent-new", "paysilo-agent-old"]

    result = verify_request(
        method="POST", authority="agent.paysilo.app", path=envelope.path, headers=envelope.headers,
        registry=registry, body=envelope.body)
    assert result.valid is True and result.key_id == "paysilo-agent-old"

    fresh = new_signer.sign_payout(
        run_id="run-9", employer_key="emp_acme", account_id="acct_1", index=0,
        line={"name": "Lena Fischer", "amount": "3400.00", "currency": "EUR", "method": "SWIFT"})
    assert 'keyid="paysilo-agent-new"' in fresh.headers["Signature-Input"]
    assert verify_request(method="POST", authority="agent.paysilo.app", path=fresh.path,
                          headers=fresh.headers, registry=registry, body=fresh.body).valid is True


# ---------------------------------------------------------------------------
# Stored-approval verification from DB-shaped rows (Issue #8)
# ---------------------------------------------------------------------------

class _Row:
    def __init__(self, key):
        self.key_id, self.agent_id, self.public_key_pem = key.key_id, key.agent_id, key.public_key_pem()


class _ApprovalRow:
    def __init__(self, envelope, run_id):
        self.run_id = run_id
        self.signed_body = envelope.body
        self.tap_signature_input = envelope.headers["Signature-Input"]
        self.tap_signature = envelope.headers["Signature"]
        self.created_at = datetime.now(UTC)


def _stored_approval():
    signer = TapSigner(key=generate_tap_agent("paysilo-agent", key_id="paysilo-agent-stored"))
    run_id = uuid.uuid4()
    envelope = signer.sign_approval(
        run_id=str(run_id), employer_key="emp_acme", decision="approve",
        assessment={"required_usd": "6754.88", "total_required": "6025.70", "payroll_currency": "EUR"},
        pay_lines=[{"name": "Lena Fischer", "amount": "3400.00", "currency": "EUR", "method": "SWIFT"}],
        decided_by="ops@example.com")
    return signer, _ApprovalRow(envelope, run_id)


def test_verify_stored_approval_from_db_rows():
    signer, approval = _stored_approval()
    registry = registry_from_tap_key_rows([_Row(signer.key)])
    result = verify_stored_approval(approval, registry)
    assert result.valid is True and result.code == "ok" and result.key_id == "paysilo-agent-stored"


def test_verify_stored_approval_tampered_body_rejected():
    signer, approval = _stored_approval()
    approval.signed_body = approval.signed_body.replace("3400.00", "9999.00")
    result = verify_stored_approval(approval, registry_from_tap_key_rows([_Row(signer.key)]))
    assert result.valid is False
    assert result.code in {"digest_mismatch", "bad_signature"}


def test_verify_stored_approval_missing_signature():
    signer, approval = _stored_approval()
    approval.tap_signature = None
    result = verify_stored_approval(approval, registry_from_tap_key_rows([_Row(signer.key)]))
    assert result.valid is False and result.code == "missing_signature"


def test_verify_stored_approval_unknown_key_rejected():
    _, approval = _stored_approval()
    other = generate_tap_agent("paysilo-agent", key_id="paysilo-agent-other")
    result = verify_stored_approval(approval, registry_from_tap_key_rows([_Row(other)]))
    assert result.valid is False and result.code == "unregistered_agent"
    assert TAP_ALGORITHM == "ed25519"


# ---------------------------------------------------------------------------
# API: /.well-known/tap-keys + POST /tap/verify (Issue #8)
# ---------------------------------------------------------------------------

def _settled_run_with_approval(client) -> dict:
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    employers = {e["name"]: e for e in client.get("/employers").json()}
    acme = client.get(f"/employers/{employers['Acme Manufacturing GmbH']['id']}").json()
    run = client.post("/runs", json={"employer_id": acme["id"],
                                     "lines": [{"contractor_id": c["id"]} for c in acme["contractors"]]}).json()
    return client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()


def test_well_known_tap_keys_publishes_active_key(demo_client):
    body = demo_client.get("/.well-known/tap-keys").json()
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert key["algorithm"] == "ed25519" and key["status"] == "active"
    assert key["keyid"] and key["public_key"] and "BEGIN PUBLIC KEY" in key["public_key_pem"]


def test_tap_verify_endpoint_verifies_stored_approval(demo_client):
    done = _settled_run_with_approval(demo_client)
    by_run = demo_client.post("/tap/verify", json={"run_id": done["id"]})
    assert by_run.status_code == 200, by_run.text
    result = by_run.json()
    assert result["verified"] is True and result["code"] == "ok"
    assert result["keyid"] == done["approvals"][0]["tap_key_id"]
    assert result["run_id"] == done["id"]


def test_tap_verify_requires_auth(client):
    assert client.post("/tap/verify", json={"run_id": str(uuid.uuid4())}).status_code == 401


def test_tap_verify_requires_exactly_one_id(demo_client):
    both = demo_client.post("/tap/verify", json={"run_id": str(uuid.uuid4()),
                                                 "approval_id": str(uuid.uuid4())})
    assert both.status_code == 422
    neither = demo_client.post("/tap/verify", json={})
    assert neither.status_code == 422


def test_tap_verify_other_tenant_gets_404(demo_client):
    done = _settled_run_with_approval(demo_client)
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Other Org", "country": "gb", "base_currency": "usd"},
        "admin": {"full_name": "Olive Other", "email": "olive@other.io", "password": "Other2026x"}})
    r = demo_client.post("/tap/verify", json={"run_id": done["id"]})
    assert r.status_code == 404  # tenant isolation: not a verification result
