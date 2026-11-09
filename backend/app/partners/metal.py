"""Metal settlement attestation — reconciliation receipt anchoring.

Interface + two implementations of PaySilo's
``SettlementChain`` design:

- :class:`MockSettlementChain` — offline, deterministic, hash-linked
  receipts. Tamper-evident: changing any receipt breaks ``verify_chain()``
  at that index. Only hashes go on-chain — never raw financial data.
- :class:`MetalL2SettlementChain` — the live Metal L2 leg: it anchors the
  receipt hash in the ``SettlementRegistry`` contract (see
  ``contracts/SettlementRegistry.sol``) over raw JSON-RPC, signing the
  transaction locally. **Fails closed** until ``METAL_RPC_URL`` /
  ``METAL_PRIVATE_KEY`` / ``METAL_REGISTRY_ADDRESS`` are configured, so a
  demo can never silently pretend to be on-chain, and it never falls back
  to the mock chain.

Only the 32-byte receipt hash goes on-chain — no amounts, names or account
details are ever sent to the chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

GENESIS_HASH = "0" * 64
# anchor(bytes32) is a single SSTORE + event; 100k gas is comfortably above
# the ~45k it needs and avoids an eth_estimateGas round-trip.
ANCHOR_GAS_LIMIT = 100_000


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class SettlementReceipt:
    index: int
    payload_hash: str
    prev_hash: str
    receipt_hash: str
    chain: str  # "mock" | "metal-l2"
    tx_hash: str | None = None  # set only when anchored on Metal L2
    explorer_url: str | None = None  # explorer link for tx_hash, when configured


class SettlementChain(ABC):
    @abstractmethod
    def attest(self, payload: dict) -> SettlementReceipt:
        """Anchor a hash of ``payload`` and return the receipt."""

    @abstractmethod
    def verify_chain(self) -> bool:
        """True when every receipt still links to its predecessor."""


class _ChainState:
    """Shared hash-chain bookkeeping, including continuation after a restart."""

    receipts: list[SettlementReceipt]
    _seed_prev: str = GENESIS_HASH
    _seed_index: int = 0

    def seed(self, prev_hash: str, index: int) -> None:
        """Continue an existing chain: the next receipt gets ``index`` and
        links to ``prev_hash`` (the last persisted receipt hash). Only valid
        on a chain with no receipts of its own yet."""
        if self.receipts:
            raise ValueError("seed() must be called before any receipt is attested.")
        if len(prev_hash) != 64 or int(prev_hash, 16) < 0:
            raise ValueError("prev_hash must be a 32-byte hex digest.")
        if index < 0:
            raise ValueError("index must be >= 0.")
        self._seed_prev = prev_hash
        self._seed_index = index

    def _next_link(self) -> tuple[int, str]:
        if self.receipts:
            return self.receipts[-1].index + 1, self.receipts[-1].receipt_hash
        return self._seed_index, self._seed_prev

    def _verify_links(self) -> bool:
        prev = self._seed_prev
        for i, receipt in enumerate(self.receipts):
            if receipt.index != self._seed_index + i or receipt.prev_hash != prev:
                return False
            if receipt.receipt_hash != _hash(f"{receipt.prev_hash}:{receipt.payload_hash}"):
                return False
            prev = receipt.receipt_hash
        return True


class MockSettlementChain(_ChainState, SettlementChain):
    """Deterministic, hash-linked, offline chain — same interface as live."""

    def __init__(self) -> None:
        self.receipts: list[SettlementReceipt] = []

    def attest(self, payload: dict) -> SettlementReceipt:
        payload_hash = _hash(canonical_payload(payload))
        index, prev_hash = self._next_link()
        receipt_hash = _hash(f"{prev_hash}:{payload_hash}")
        receipt = SettlementReceipt(
            index=index,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            receipt_hash=receipt_hash,
            chain="mock",
        )
        self.receipts.append(receipt)
        return receipt

    def verify_chain(self) -> bool:
        return self._verify_links()


class MetalNotConfiguredError(RuntimeError):
    """Metal L2 was asked to attest without full configuration."""


class MetalAttestationPending(RuntimeError):
    """The anchor tx was broadcast but no successful receipt was seen in time.

    The hash is NOT claimed as anchored. ``tx_hash`` lets an operator check
    the explorer and re-attest later.
    """

    def __init__(self, message: str, tx_hash: str | None = None):
        super().__init__(message)
        self.tx_hash = tx_hash


class MetalRPCError(RuntimeError):
    """The Metal L2 JSON-RPC endpoint returned an error or a bad result."""


def anchor_calldata(receipt_hash: str) -> bytes:
    """Calldata for ``SettlementRegistry.anchor(bytes32)`` — hash only."""
    from eth_utils import keccak

    digest = bytes.fromhex(receipt_hash)
    if len(digest) != 32:
        raise ValueError("receipt_hash must be a 32-byte hex digest.")
    return keccak(text="anchor(bytes32)")[:4] + digest


class MetalL2SettlementChain(_ChainState, SettlementChain):
    """Live Metal L2 attestation via raw JSON-RPC. Fails closed until configured.

    The transaction calls ``anchor(bytes32)`` on the deployed
    ``SettlementRegistry`` with exactly one argument — the receipt hash —
    and is signed locally with the configured testnet wallet key
    (EIP-1559 when the chain reports a base fee, legacy otherwise). No
    web3.py: JSON-RPC goes over ``httpx`` and signing uses ``eth-account``.

    ``transport`` may inject an ``httpx`` transport (tests use
    ``httpx.MockTransport``); by default real HTTP is used.
    """

    def __init__(self, rpc_url: str | None = None, private_key: str | None = None,
                 registry_address: str | None = None, explorer_url: str | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float = 15.0,
                 receipt_timeout: float = 60.0, poll_interval: float = 2.0) -> None:
        self.rpc_url = rpc_url if rpc_url is not None else os.environ.get("METAL_RPC_URL")
        self.private_key = private_key if private_key is not None else os.environ.get("METAL_PRIVATE_KEY")
        self.registry_address = (
            registry_address if registry_address is not None else os.environ.get("METAL_REGISTRY_ADDRESS")
        )
        self.explorer_url = explorer_url if explorer_url is not None else os.environ.get("METAL_EXPLORER_URL")
        self.transport = transport
        self.timeout = timeout
        self.receipt_timeout = receipt_timeout
        self.poll_interval = poll_interval
        self.receipts: list[SettlementReceipt] = []
        self._request_id = 0

    @property
    def configured(self) -> bool:
        return bool(self.rpc_url and self.private_key and self.registry_address)

    def _require_configured(self) -> None:
        if not self.configured:
            raise MetalNotConfiguredError(
                "Metal L2 is not configured: set METAL_RPC_URL, METAL_PRIVATE_KEY and "
                "METAL_REGISTRY_ADDRESS (faucet-funded testnet wallet) to enable live attestation. "
                "Failing closed — no anchor was made and the mock chain was NOT used."
            )

    def explorer_url_for(self, tx_hash: str | None) -> str | None:
        if not tx_hash or not self.explorer_url:
            return None
        return f"{self.explorer_url.rstrip('/')}/tx/{tx_hash}"

    # ------------------------------------------------------------- JSON-RPC
    def _rpc(self, method: str, params: list) -> Any:
        self._request_id += 1
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            response = client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params})
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise MetalRPCError(f"{method} failed: {data['error']}")
        if "result" not in data:
            raise MetalRPCError(f"{method} returned no result: {data!r}")
        return data["result"]

    def _wait_for_success(self, tx_hash: str) -> None:
        """Poll ``eth_getTransactionReceipt`` until the tx is mined.

        ``status == 0x1`` -> anchored. ``0x0`` -> reverted (MetalRPCError).
        No receipt before ``receipt_timeout`` -> MetalAttestationPending.
        """
        deadline = time.monotonic() + self.receipt_timeout
        while True:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = receipt.get("status")
                if status is not None and int(status, 16) == 1:
                    return
                raise MetalRPCError(f"Anchor transaction {tx_hash} was mined but did not succeed "
                                    f"(status={status!r}).")
            if time.monotonic() >= deadline:
                raise MetalAttestationPending(
                    f"Anchor transaction {tx_hash} was sent but not confirmed within "
                    f"{self.receipt_timeout:g}s; NOT treated as anchored.", tx_hash=tx_hash)
            time.sleep(self.poll_interval)

    def _account(self):
        from eth_account import Account

        return Account.from_key(self.private_key)

    def _build_anchor_tx(self, receipt_hash: str) -> dict:
        address = self._account().address
        tx: dict[str, Any] = {
            "to": self.registry_address,
            "value": 0,
            "gas": ANCHOR_GAS_LIMIT,
            "nonce": int(self._rpc("eth_getTransactionCount", [address, "pending"]), 16),
            "chainId": int(self._rpc("eth_chainId", []), 16),
            "data": anchor_calldata(receipt_hash),
        }
        try:  # EIP-1559 when the chain reports the fee fields.
            priority = int(self._rpc("eth_maxPriorityFeePerGas", []), 16)
            block = self._rpc("eth_getBlockByNumber", ["latest", False])
            base_fee = int(block["baseFeePerGas"], 16)
            tx.update({"type": 2, "maxPriorityFeePerGas": priority,
                       "maxFeePerGas": 2 * base_fee + priority})
        except Exception:  # legacy pricing fallback (older / simpler chains)
            tx.update({"gasPrice": int(self._rpc("eth_gasPrice", []), 16)})
        return tx

    # ------------------------------------------------------------ interface
    def attest(self, payload: dict) -> SettlementReceipt:
        self._require_configured()
        payload_hash = _hash(canonical_payload(payload))
        index, prev_hash = self._next_link()
        receipt_hash = _hash(f"{prev_hash}:{payload_hash}")

        signed = self._account().sign_transaction(self._build_anchor_tx(receipt_hash))
        raw_hex = "0x" + signed.raw_transaction.hex()
        tx_hash = self._rpc("eth_sendRawTransaction", [raw_hex])
        if not (isinstance(tx_hash, str) and tx_hash.startswith("0x") and len(tx_hash) == 66):
            raise MetalRPCError(f"eth_sendRawTransaction returned an invalid tx hash: {tx_hash!r}")
        self._wait_for_success(tx_hash)

        receipt = SettlementReceipt(
            index=index,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            receipt_hash=receipt_hash,
            chain="metal-l2",
            tx_hash=tx_hash,
            explorer_url=self.explorer_url_for(tx_hash),
        )
        self.receipts.append(receipt)
        return receipt

    def verify_chain(self) -> bool:
        """Local hash-linkage check over the receipts this process anchored.

        (On-chain existence of a hash can be checked independently with
        ``SettlementRegistry.isAnchored`` / the explorer link.) Fails closed
        when unconfigured, like every other live-leg call.
        """
        self._require_configured()
        return self._verify_links()
