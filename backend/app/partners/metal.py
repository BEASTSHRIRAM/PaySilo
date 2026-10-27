"""Metal settlement attestation — reconciliation receipt anchoring.

Interface + two implementations of PaySilo's
``SettlementChain`` design:

- :class:`MockSettlementChain` — offline, deterministic, hash-linked
  receipts. Tamper-evident: changing any receipt breaks ``verify_chain()``
  at that index. Only hashes go on-chain — never raw financial data.
- :class:`MetalL2SettlementChain` — the live Metal L2 leg. **Fails closed**
  until ``METAL_RPC_URL`` / ``METAL_PRIVATE_KEY`` / ``METAL_REGISTRY_ADDRESS``
  are configured, so a demo can never silently pretend to be on-chain.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

GENESIS_HASH = "0" * 64


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


class SettlementChain(ABC):
    @abstractmethod
    def attest(self, payload: dict) -> SettlementReceipt:
        """Anchor a hash of ``payload`` and return the receipt."""

    @abstractmethod
    def verify_chain(self) -> bool:
        """True when every receipt still links to its predecessor."""


class MockSettlementChain(SettlementChain):
    """Deterministic, hash-linked, offline chain — same interface as live."""

    def __init__(self) -> None:
        self.receipts: list[SettlementReceipt] = []

    def attest(self, payload: dict) -> SettlementReceipt:
        payload_hash = _hash(canonical_payload(payload))
        prev_hash = self.receipts[-1].receipt_hash if self.receipts else GENESIS_HASH
        receipt_hash = _hash(f"{prev_hash}:{payload_hash}")
        receipt = SettlementReceipt(
            index=len(self.receipts),
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            receipt_hash=receipt_hash,
            chain="mock",
        )
        self.receipts.append(receipt)
        return receipt

    def verify_chain(self) -> bool:
        prev = GENESIS_HASH
        for i, receipt in enumerate(self.receipts):
            if receipt.index != i or receipt.prev_hash != prev:
                return False
            if receipt.receipt_hash != _hash(f"{receipt.prev_hash}:{receipt.payload_hash}"):
                return False
            prev = receipt.receipt_hash
        return True


class MetalNotConfiguredError(RuntimeError):
    pass


class MetalL2SettlementChain(SettlementChain):
    """Live Metal L2 attestation. Fails closed until configured.

    The build-phase live leg (deploy ``SettlementRegistry``, submit the
    receipt hash via the chain RPC) is intentionally not faked here: with
    no env configured every call raises, and with env configured it raises
    NotImplementedError until the registry contract leg is wired. A demo
    must never claim an on-chain anchor it did not make.
    """

    def __init__(self, rpc_url: str | None = None, private_key: str | None = None,
                 registry_address: str | None = None) -> None:
        self.rpc_url = rpc_url if rpc_url is not None else os.environ.get("METAL_RPC_URL")
        self.private_key = private_key if private_key is not None else os.environ.get("METAL_PRIVATE_KEY")
        self.registry_address = (
            registry_address if registry_address is not None else os.environ.get("METAL_REGISTRY_ADDRESS")
        )

    @property
    def configured(self) -> bool:
        return bool(self.rpc_url and self.private_key and self.registry_address)

    def _fail_closed(self) -> None:
        if not self.configured:
            raise MetalNotConfiguredError(
                "Metal L2 is not configured: set METAL_RPC_URL, METAL_PRIVATE_KEY and "
                "METAL_REGISTRY_ADDRESS (faucet-funded testnet wallet) to enable live attestation. "
                "Failing closed — no anchor was made."
            )
        raise NotImplementedError(
            "Metal L2 live submission leg is not wired in this build; use MockSettlementChain "
            "for demos. Env is configured, so wiring is: deploy SettlementRegistry, then submit "
            "receipt_hash via RPC and return the tx hash as the receipt."
        )

    def attest(self, payload: dict) -> SettlementReceipt:
        self._fail_closed()
        raise AssertionError("unreachable")  # pragma: no cover

    def verify_chain(self) -> bool:
        self._fail_closed()
        return False  # pragma: no cover
