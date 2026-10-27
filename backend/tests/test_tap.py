import time

from app.partners.tap import (
    TapAgentRegistry,
    TapReplayGuard,
    generate_tap_agent,
    sign_request,
    verify_request,
)

AUTHORITY = "api-demo.airwallex.com"
PATH = "/api/v1/transfers/create"
BODY = '{"employer":"Acme Manufacturing GmbH","amount":"3400.00","currency":"EUR"}'


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
