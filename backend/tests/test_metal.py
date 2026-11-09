"""Metal L2 attestation (Issue #9): fail-closed config, mocked JSON-RPC
transport, hashes-only on-chain. No test needs a real chain or keys."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.partners.metal import (
    MetalL2SettlementChain,
    MetalNotConfiguredError,
    MockSettlementChain,
    anchor_calldata,
)

# Public, well-known Hardhat test account #0 — a test key, never a real wallet.
TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
REGISTRY = "0x1111111111111111111111111111111111111111"
TX_HASH = "0x" + "ab" * 32

PAYLOAD = {
    "run_id": "run-secret-1",
    "employer": "Borealis Labs AB",
    "beneficiary": "Ingrid Solberg",
    "amount": "6012.85",
    "iban": "SE4550000000058398257466",
    "status": "SETTLED",
}


def _mock_transport(captured: list, overrides: dict | None = None) -> httpx.MockTransport:
    results = {
        "eth_chainId": "0x270f",
        "eth_getTransactionCount": "0x0",
        "eth_maxPriorityFeePerGas": "0x3b9aca00",
        "eth_gasPrice": "0x3b9aca00",
        "eth_getBlockByNumber": {"number": "0x1", "baseFeePerGas": "0x3b9aca00"},
        "eth_sendRawTransaction": TX_HASH,
        "eth_getTransactionReceipt": {"status": "0x1", "transactionHash": TX_HASH},
    }
    results.update(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((body["method"], body.get("params", [])))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": results[body["method"]]})

    return httpx.MockTransport(handler)


def _l2_chain(captured: list, **overrides) -> MetalL2SettlementChain:
    kwargs = dict(rpc_url="https://rpc.invalid", private_key=TEST_PRIVATE_KEY,
                  registry_address=REGISTRY, explorer_url="https://explorer.example/",
                  transport=_mock_transport(captured))
    kwargs.update(overrides)
    return MetalL2SettlementChain(**kwargs)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_unconfigured_l2_fails_closed():
    chain = MetalL2SettlementChain(rpc_url="", private_key="", registry_address="")
    assert chain.configured is False
    with pytest.raises(MetalNotConfiguredError):
        chain.attest(PAYLOAD)
    with pytest.raises(MetalNotConfiguredError):
        chain.verify_chain()


def test_partially_configured_l2_fails_closed():
    chain = MetalL2SettlementChain(rpc_url="https://rpc.invalid", private_key="", registry_address="")
    with pytest.raises(MetalNotConfiguredError):
        chain.attest(PAYLOAD)


def test_unconfigured_env_fails_closed(monkeypatch):
    for name in ("METAL_RPC_URL", "METAL_PRIVATE_KEY", "METAL_REGISTRY_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    chain = MetalL2SettlementChain()
    assert chain.configured is False
    with pytest.raises(MetalNotConfiguredError):
        chain.attest(PAYLOAD)


# ---------------------------------------------------------------------------
# Happy path over a mocked JSON-RPC transport
# ---------------------------------------------------------------------------

def test_attest_happy_path_returns_and_stores_tx_hash():
    captured: list = []
    chain = _l2_chain(captured)
    receipt = chain.attest(PAYLOAD)

    assert receipt.chain == "metal-l2"
    assert receipt.tx_hash == TX_HASH
    assert receipt.explorer_url == f"https://explorer.example/tx/{TX_HASH}"
    assert chain.receipts[-1].tx_hash == TX_HASH  # stored on the receipt
    assert chain.verify_chain() is True

    methods = [m for m, _ in captured]
    assert methods[-2:] == ["eth_sendRawTransaction", "eth_getTransactionReceipt"]
    assert "eth_chainId" in methods and "eth_getTransactionCount" in methods

    # The signed transaction really is signed by the configured wallet and
    # really is addressed to the registry contract.
    from eth_account import Account

    raw_hex = dict((m, p) for m, p in captured)["eth_sendRawTransaction"][0]
    assert Account.recover_transaction(raw_hex) == TEST_ADDRESS
    raw = bytes.fromhex(raw_hex[2:])
    assert bytes.fromhex(REGISTRY[2:]) in raw


def test_attest_sends_only_the_receipt_hash_no_pii():
    captured: list = []
    chain = _l2_chain(captured)
    receipt = chain.attest(PAYLOAD)

    raw_hex = dict((m, p) for m, p in captured)["eth_sendRawTransaction"][0]
    raw = bytes.fromhex(raw_hex[2:])
    # The one and only payload-derived value on-chain: the receipt hash,
    # as the anchor(bytes32) argument.
    assert anchor_calldata(receipt.receipt_hash) in raw
    assert bytes.fromhex(receipt.receipt_hash) in raw
    # Nothing else about the payroll leaks into the transaction.
    for secret in (b"Ingrid", b"Borealis", b"6012", b"SE455000", b"run-secret"):
        assert secret not in raw


def test_attest_chains_receipts_like_the_mock():
    captured: list = []
    chain = _l2_chain(captured)
    first = chain.attest(PAYLOAD)
    second = chain.attest({**PAYLOAD, "run_id": "run-secret-2"})
    assert second.prev_hash == first.receipt_hash and second.index == 1
    assert first.receipt_hash != second.receipt_hash


def test_rpc_error_surfaces_instead_of_a_fake_anchor():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "error": {"code": -32000, "message": "insufficient funds"}})

    chain = _l2_chain([], transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as excinfo:
        chain.attest(PAYLOAD)
    assert "insufficient funds" in str(excinfo.value)
    assert chain.receipts == []  # nothing was labelled anchored


# ---------------------------------------------------------------------------
# Mock stays the default
# ---------------------------------------------------------------------------

def test_mock_chain_is_the_default_and_labels_itself_mock():
    from app.agents.demo import build_demo_services

    assert isinstance(build_demo_services().chain, MockSettlementChain)
    receipt = MockSettlementChain().attest(PAYLOAD)
    assert receipt.chain == "mock" and receipt.tx_hash is None and receipt.explorer_url is None


def test_graph_attest_with_l2_chain_puts_tx_hash_on_the_receipt():
    from app.agents.demo import EMPLOYERS, build_demo_services, drive_run
    from app.agents.graph import build_graph, initial_state

    captured: list = []
    services = build_demo_services()
    services.chain = _l2_chain(captured)
    graph = build_graph(services)
    spec = EMPLOYERS["acme"]
    final = drive_run(graph, services, initial_state(
        run_id="run-l2", employer_key="acme", employer_name=spec["name"],
        contact_name=spec["contact"], funding_usd=spec["funding_usd"],
        pay_lines=spec["pay_lines"]), "run-l2")
    receipt = final["metal_receipt"]
    assert receipt["chain"] == "metal-l2" and receipt["tx_hash"] == TX_HASH
    assert receipt["explorer_url"] == f"https://explorer.example/tx/{TX_HASH}"


def test_explorer_url_field_for_the_run_panel():
    from app.api.serializers import _explorer_url

    runtime = SimpleNamespace(settings=SimpleNamespace(metal_explorer_url="https://explorer.example/"))
    assert _explorer_url(runtime, TX_HASH) == f"https://explorer.example/tx/{TX_HASH}"
    assert _explorer_url(runtime, None) is None
    blank = SimpleNamespace(settings=SimpleNamespace(metal_explorer_url=None))
    assert _explorer_url(blank, TX_HASH) is None


# -- receipt polling: never claim anchored before status 0x1 ------------------------

def test_attest_polls_until_receipt_status_success(monkeypatch):
    monkeypatch.setattr("app.partners.metal.time.sleep", lambda _s: None)
    seq = iter([None, None, {"status": "0x1"}])
    captured: list = []
    chain = _l2_chain(captured)
    real = chain._rpc

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            captured.append((method, params))
            return next(seq)
        return real(method, params)

    chain._rpc = rpc
    receipt = chain.attest(PAYLOAD)
    assert receipt.tx_hash == TX_HASH
    assert [m for m, _ in captured].count("eth_getTransactionReceipt") == 3


def test_attest_pending_receipt_times_out_and_records_nothing(monkeypatch):
    from app.partners.metal import MetalAttestationPending

    monkeypatch.setattr("app.partners.metal.time.sleep", lambda _s: None)
    chain = _l2_chain([], transport=_mock_transport([], {"eth_getTransactionReceipt": None}),
                      receipt_timeout=0.0)
    with pytest.raises(MetalAttestationPending) as exc:
        chain.attest(PAYLOAD)
    assert exc.value.tx_hash == TX_HASH
    assert chain.receipts == []


def test_attest_reverted_tx_is_an_error_not_an_anchor():
    from app.partners.metal import MetalRPCError

    chain = _l2_chain([], transport=_mock_transport([], {"eth_getTransactionReceipt": {"status": "0x0"}}))
    with pytest.raises(MetalRPCError, match="did not succeed"):
        chain.attest(PAYLOAD)
    assert chain.receipts == []


# -- seed(): continue the hash chain after a restart ------------------------------------

def test_mock_chain_seed_continues_hash_chain():
    first = MockSettlementChain()
    r0 = first.attest({"a": 1})
    restarted = MockSettlementChain()
    restarted.seed(r0.receipt_hash, 1)
    r1 = restarted.attest({"a": 2})
    assert r1.index == 1 and r1.prev_hash == r0.receipt_hash
    assert restarted.verify_chain() is True
    reference = MockSettlementChain()
    reference.attest({"a": 1})
    assert reference.attest({"a": 2}).receipt_hash == r1.receipt_hash


def test_l2_chain_seed_continues_hash_chain():
    chain = _l2_chain([])
    chain.seed("ab" * 32, 5)
    r = chain.attest(PAYLOAD)
    assert r.index == 5 and r.prev_hash == "ab" * 32
    assert chain.verify_chain() is True
    with pytest.raises(ValueError):
        chain.seed("cd" * 32, 9)  # cannot re-seed after attesting
