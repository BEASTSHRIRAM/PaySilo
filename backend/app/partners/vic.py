"""Visa Intelligent Commerce (VIC) via Airi — Silo Benefits purchases.

Interface + two implementations, mirroring :mod:`app.partners.metal`:

- :class:`SimulatedVicAdapter` — offline, deterministic. Used whenever no
  VIC credential is configured. Every result is labelled ``SIMULATED``
  and the credential reference is the masked published test card
  (``4111…1111``) — never a raw card number.
- :class:`AiriVicAdapter` — the live leg: it shells out to the Airi CLI
  with the configured VIC credential in the environment. **Fails closed**:
  when a credential is set but the CLI is not available it raises
  :class:`VicUnavailable` rather than falling back to simulation, so a
  live configuration can never silently pretend to be real.

Only masked credential references are ever stored or logged. The raw
credential is passed to the CLI through its environment only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..money import money_str

# The test card's per-transaction limit (USD). Enforced in the benefits
# service and again here, as defence in depth.
TEST_CARD_TX_LIMIT_USD = Decimal("200.00")


@dataclass(frozen=True)
class VicPurchaseResult:
    ok: bool
    provider_ref: str | None
    failure_code: str | None = None
    failure_detail: str | None = None


class VicUnavailable(Exception):
    """A live VIC purchase was requested but the live path is unavailable."""


class VicAdapter(Protocol):
    """The narrow interface the benefits service depends on."""

    mode: str
    credential_ref: str
    test_card: bool
    cli_available: bool

    def purchase(self, *, merchant: str, amount: Decimal, currency: str,
                 idempotency_key: str) -> VicPurchaseResult:
        ...


class SimulatedVicAdapter:
    """Deterministic, offline VIC simulation — honestly labelled everywhere."""

    mode = "SIMULATED"
    credential_ref = "vic_sim_••••1111"  # published test card 4111…1111, masked only
    test_card = True
    cli_available = True

    def purchase(self, *, merchant: str, amount: Decimal, currency: str,
                 idempotency_key: str) -> VicPurchaseResult:
        if amount > TEST_CARD_TX_LIMIT_USD:
            return VicPurchaseResult(
                ok=False, provider_ref=None, failure_code="TX_LIMIT_EXCEEDED",
                failure_detail=(f"Simulated test card limit is USD {money_str(TEST_CARD_TX_LIMIT_USD)} "
                                f"per transaction; {currency} {money_str(amount)} exceeds it."))
        if "decline" in merchant.lower():
            # Documented test hook: any merchant whose name contains
            # "decline" is declined by the simulated provider.
            return VicPurchaseResult(
                ok=False, provider_ref=None, failure_code="PROVIDER_DECLINED",
                failure_detail="The simulated provider declined this purchase (test hook: "
                               "merchant name contains 'decline').")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
        return VicPurchaseResult(ok=True, provider_ref=f"vic_sim_{digest}")


class AiriVicAdapter:
    """Live VIC purchases through the Airi CLI. Fails closed.

    The CLI argument shape below is a conservative placeholder pending the
    real airi-cli interface: ``airi purchase --merchant … --amount …
    --currency … --idempotency-key …`` with ``VIC_CREDENTIAL`` in the
    environment, expecting a JSON object with ``provider_ref`` on stdout.
    Until that interface is confirmed, this path fails closed — a missing
    CLI raises :class:`VicUnavailable` and a failing/invalid CLI response
    is a failed purchase, never a simulated success.
    """

    mode = "VIC"

    def __init__(self, settings: Settings) -> None:
        self._credential = settings.vic_credential or ""
        self._cli_path = settings.airi_cli_path
        self.test_card = settings.vic_test_card
        # A fingerprint of the credential — never the credential itself,
        # and never logged. Safe to store on purchase rows.
        fingerprint = hashlib.sha256(self._credential.encode("utf-8")).hexdigest()[:8]
        self.credential_ref = f"airi_vic_{fingerprint}"
        self.cli_available = bool(
            self._cli_path
            and Path(self._cli_path).is_file()
            and os.access(self._cli_path, os.X_OK)
        )

    def purchase(self, *, merchant: str, amount: Decimal, currency: str,
                 idempotency_key: str) -> VicPurchaseResult:
        if not self.cli_available:
            raise VicUnavailable(
                "VIC_CREDENTIAL is set but the Airi CLI is not available "
                "(set AIRI_CLI_PATH). Refusing to fall back to simulation.")
        try:
            proc = subprocess.run(
                [self._cli_path, "purchase", "--merchant", merchant,
                 "--amount", money_str(amount), "--currency", currency,
                 "--idempotency-key", idempotency_key],
                env={**os.environ, "VIC_CREDENTIAL": self._credential},
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return VicPurchaseResult(
                ok=False, provider_ref=None, failure_code="VIC_CLI_FAILED",
                failure_detail=f"Airi CLI could not be run: {exc}"[:500])
        if proc.returncode != 0:
            return VicPurchaseResult(
                ok=False, provider_ref=None, failure_code="VIC_CLI_FAILED",
                failure_detail=(proc.stderr or "Airi CLI exited non-zero.").strip()[:500])
        try:
            payload = json.loads(proc.stdout)
            provider_ref = payload["provider_ref"]
            if not isinstance(provider_ref, str) or not provider_ref:
                raise ValueError("empty provider_ref")
        except (ValueError, KeyError, TypeError):
            return VicPurchaseResult(
                ok=False, provider_ref=None, failure_code="VIC_CLI_FAILED",
                failure_detail="Airi CLI returned an invalid response.")
        return VicPurchaseResult(ok=True, provider_ref=provider_ref)


def build_vic_adapter(settings: Settings) -> VicAdapter:
    """Credential set → live Airi adapter; otherwise the simulated adapter."""
    if settings.vic_credential:
        return AiriVicAdapter(settings)
    return SimulatedVicAdapter()


def vic_status(settings: Settings, adapter: VicAdapter) -> dict:
    """Public status for ``GET /benefits/status`` — mode is always explicit."""
    if not settings.vic_credential:
        mode = "SIMULATED"
    elif adapter.cli_available:
        mode = "VIC"
    else:
        mode = "VIC_UNAVAILABLE"
    return {
        "mode": mode,
        "credential_ref": adapter.credential_ref,
        "test_card": adapter.test_card,
        "tx_limit_usd": money_str(TEST_CARD_TX_LIMIT_USD) if adapter.test_card else None,
        "cli_available": adapter.cli_available,
    }
