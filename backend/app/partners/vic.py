"""Visa Intelligent Commerce (VIC) via Airi — Silo Benefits purchases.

Interface + two implementations, mirroring :mod:`app.partners.metal`:

- :class:`SimulatedVicAdapter` — offline, deterministic. Used when
  ``VIC_MODE=simulated`` (the default). Every result is labelled
  ``SIMULATED`` and the credential reference is the masked published test
  card (``4111…1111``) — never a raw card number.
- :class:`AiriVicAdapter` — the live leg, built on the REAL ``airi-cli``
  (``@airwallex/airi-cli`` v0.1.0-beta.x). **Fails closed**: when
  ``VIC_MODE=live`` but the CLI is missing or signed out it raises
  :class:`VicUnavailable` rather than falling back to simulation.

The live flow is TWO-PHASE so the human stays in the loop (model reads, code
decides, human approves):

A. :meth:`AiriVicAdapter.create_mandate` — ``payment-methods list`` then
   ``mandate create``. Returns the mandate id and the passkey URL the HUMAN
   must open and approve in their browser. Nothing is spent.
B. :meth:`AiriVicAdapter.complete` — only on an explicit human click. Runs
   ``mandate get-credential --file <0600 temp path>`` (polls until the human
   authorized), keeps only the credential RECEIPT (status, tokenType,
   expiry), asks a pluggable :class:`CheckoutExecutor` for the checkout
   outcome, deletes the credential file, and ALWAYS reports
   ``mandate result``.

Card data (tokenized PAN, expiry, CVV) is never read by this module: the CLI
writes it to a private file we never open, and the file is deleted afterwards.
Nothing card-shaped is stored, logged or sent to a model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..money import money_str

logger = logging.getLogger(__name__)

# The test card's per-transaction limit (USD). Enforced in the benefits
# service and again here, as defence in depth (before any CLI call).
TEST_CARD_TX_LIMIT_USD = Decimal("200.00")

AWAITING_AUTH = "AWAITING_USER_AUTHORIZATION"
AGENT_SOURCE = "paysilo"
MANDATE_STATES = (AWAITING_AUTH, "AUTHORIZED", "COMPLETED", "CANCELLED", "DECLINED", "FAILED")

_RECEIPT_KEYS = ("status", "tokenType", "tokenExpiration")
_LONG_DIGITS = re.compile(r"\d{9,}")


@dataclass(frozen=True)
class VicPurchaseResult:
    ok: bool
    provider_ref: str | None
    failure_code: str | None = None
    failure_detail: str | None = None


class VicUnavailable(Exception):
    """A live VIC purchase was requested but the live path is unavailable."""


class VicError(Exception):
    """A live VIC call failed in a way the human should see (no card data)."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class MandateInfo:
    mandate_id: str
    approval_url: str
    payment_method_ref: str  # masked/short reference, never card data


@dataclass(frozen=True)
class CheckoutOutcome:
    result: str  # SUCCEEDED | CANCELLED | DECLINED
    reason: str | None = None


@dataclass(frozen=True)
class CompleteResult:
    """Outcome of phase B. ``state`` is one of :data:`MANDATE_STATES`."""

    state: str
    ok: bool = False
    receipt: dict = field(default_factory=dict)  # status / tokenType / tokenExpiration only
    failure_code: str | None = None
    failure_detail: str | None = None
    result_reported: bool = False


class CheckoutExecutor(Protocol):
    """Pluggable merchant-checkout step.

    PaySilo has no merchant checkout in this hackathon build. A real executor
    would read ``credential_file`` (single use, 0600) to fill a merchant form
    and report what happened. The default executor never opens the file.
    """

    def execute(self, *, mandate_id: str, merchant: str, amount: Decimal, currency: str,
                credential_file: str) -> CheckoutOutcome:
        ...


class HumanConfirmedCheckoutExecutor:
    """HONEST placeholder: no merchant is charged by PaySilo in this build.

    Reports SUCCEEDED only when the credential was obtained (it is only called
    then) AND the human confirmed the purchase happened; otherwise CANCELLED.
    It never opens the credential file and never fakes a merchant charge.
    """

    def __init__(self, human_confirmed: bool) -> None:
        self.human_confirmed = human_confirmed

    def execute(self, *, mandate_id: str, merchant: str, amount: Decimal, currency: str,
                credential_file: str) -> CheckoutOutcome:
        if self.human_confirmed:
            return CheckoutOutcome("SUCCEEDED")
        return CheckoutOutcome("CANCELLED", "The purchase was not confirmed by the human.")


class VicAdapter(Protocol):
    """The narrow interface the benefits service depends on."""

    mode: str
    credential_ref: str
    test_card: bool
    cli_available: bool
    two_phase: bool

    def purchase(self, *, merchant: str, amount: Decimal, currency: str,
                 idempotency_key: str) -> VicPurchaseResult:
        ...

    def probe(self, *, force: bool = False) -> tuple[bool, bool]:
        """(cli_present, authenticated)."""
        ...


class SimulatedVicAdapter:
    """Deterministic, offline VIC simulation — honestly labelled everywhere."""

    mode = "SIMULATED"
    credential_ref = "vic_sim_••••1111"  # published test card 4111…1111, masked only
    test_card = True
    cli_available = True
    two_phase = False

    def probe(self, *, force: bool = False) -> tuple[bool, bool]:
        return True, True

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


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #

@dataclass
class _Cli:
    returncode: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


def _safe(text: str, limit: int = 300) -> str:
    """Trim a CLI message for display and strip anything card-number-shaped."""
    return _LONG_DIGITS.sub("…", (text or "").strip())[:limit]


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys: str):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


_AIRI_HOSTS = ("airi.com", "airwallex.com")


def validate_verification_url(url: str) -> str:
    """Only https links on airi.com / airwallex.com (or their subdomains) reach the browser."""
    ok = False
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        ok = (parts.scheme == "https" and not parts.username and parts.port in (None, 443)
              and any(host == h or host.endswith("." + h) for h in _AIRI_HOSTS))
    except ValueError:
        ok = False
    if not ok:
        raise VicError("AIRI_LOGIN_REJECTED", "The Airi CLI returned a sign-in link that is not on an "
                                              "Airi or Airwallex domain, so it was refused.")
    return url.strip()


def merchant_url_for(merchant: str) -> str | None:
    """A merchant URL only when the merchant is given as a domain/URL; never invented."""
    m = merchant.strip()
    if re.match(r"^https?://\S+\.\S+$", m, re.I):
        return m
    if re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?$", m, re.I):
        return "https://" + m
    return None


class AiriVicAdapter:
    """Live VIC purchases through the real ``airi-cli``. Fails closed."""

    mode = "VIC"
    credential_ref = "airi_vic_wallet"
    two_phase = True
    _PROBE_TTL = 15.0
    _GRACE = 30.0  # extra seconds beyond the CLI's own --timeout before we kill it

    def __init__(self, settings: Settings, *, run_timeout: float = 60.0) -> None:
        self._command = shlex.split(settings.airi_cli_command or "airi-cli")
        if not self._command:
            self._command = ["airi-cli"]
        self.test_card = settings.vic_test_card
        self.merchant_country = settings.vic_merchant_country or "US"
        self._run_timeout = run_timeout
        self._probe_at = 0.0
        self._probe_val: tuple[bool, bool] = (False, False)
        self.cli_available = False
        self.session_expires: str | None = None  # RFC3339 from whoami; no other whoami field is kept
        # One Airi account per server: the in-flight login lives in process memory, globally.
        self._login_lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._pending: dict | None = None  # {uri, code, expires_at (monotonic)}

    # -- process plumbing ---------------------------------------------------
    def _argv(self, args: list[str]) -> list[str]:
        """Command + args. A token containing ``{args}`` is a shell template
        (e.g. ``bash -lc 'airi-cli {args}'`` for WSL login shells)."""
        if any("{args}" in tok for tok in self._command):
            joined = " ".join(shlex.quote(a) for a in args)
            return [tok.replace("{args}", joined) for tok in self._command]
        return [*self._command, *args]

    def _run(self, args: list[str], timeout: float | None = None) -> _Cli:
        try:
            proc = subprocess.run(self._argv(args), capture_output=True, text=True,
                                  timeout=timeout or self._run_timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise VicUnavailable("The Airi CLI was not found (check AIRI_CLI_COMMAND / install "
                                 "@airwallex/airi-cli). Refusing to fall back to simulation.") from None
        except OSError as exc:
            raise VicUnavailable(f"The Airi CLI could not be run: {_safe(str(exc))}") from None
        return _Cli(proc.returncode, proc.stdout or "", proc.stderr or "")

    # -- connect / disconnect (server-side Airi sign-in) ---------------------
    # Only ``authenticated`` and ``session_expires`` are read from ``whoami``;
    # personal fields (email, phone) are never kept. The device code and the
    # verification URL are never logged.
    def auth_status(self) -> dict:
        present, authed = self.probe(force=True)
        return {"cli_available": present, "authenticated": authed,
                "session_expires": self.session_expires if authed else None,
                "pending": self._pending_view() is not None}

    def _pending_view(self) -> dict | None:
        with self._login_lock:
            p = self._pending
            if p and time.monotonic() >= p["expires_at"]:
                self._pending = None
                return None
            return p

    @staticmethod
    def _pending_out(p: dict) -> dict:
        return {"state": "pending", "verification_uri": p["uri"], "user_code": p["code"],
                "expires_in": max(0, int(p["expires_at"] - time.monotonic()))}

    def _connected(self) -> dict:
        self.probe(force=True)
        return {"state": "connected", "session_expires": self.session_expires}

    def start_login(self, client_name: str = "PaySilo") -> dict:
        """Begin (or reuse) a non-blocking device login. Returns immediately."""
        with self._login_lock:
            present, authed = self.probe(force=True)
            if not present:
                raise VicUnavailable("The Airi CLI is not available on this server "
                                     "(check AIRI_CLI_COMMAND / install @airwallex/airi-cli).")
            if authed:
                self._pending = None
                return {"state": "connected", "session_expires": self.session_expires}
            p = self._pending_view()
            if p:  # never create a second code while one is pending
                return self._pending_out(p)
            out = self._run(["auth", "login", "--client-name", _safe(client_name, 40) or "PaySilo",
                             "-o", "json"], timeout=30)
            data = _json(out.stdout)
            if out.returncode != 0 or not isinstance(data, dict):
                raise VicError("AIRI_LOGIN_FAILED", "The Airi CLI could not start a sign-in "
                                                    "(unexpected response).")
            if data.get("authenticated") is True:
                return self._connected()
            uri = _first(data, "verification_uri_complete", "verification_uri", "verification_url")
            code = _first(data, "user_code", "code")
            if not uri or not code:
                raise VicError("AIRI_LOGIN_FAILED", "The Airi CLI returned an unexpected sign-in response.")
            uri = validate_verification_url(uri)
            raw_exp = data.get("expires_in")
            expires_in = int(raw_exp) if isinstance(raw_exp, (int, float)) and 30 <= raw_exp <= 3600 else 600
            self._pending = {"uri": uri, "code": code[:64], "expires_at": time.monotonic() + expires_in}
            return self._pending_out(self._pending)

    def poll_login(self, timeout_s: float = 8.0) -> dict:
        """One short wait for approval. The browser calls this repeatedly."""
        p = self._pending_view()
        if p is None:
            present, authed = self.probe(force=True)
            if not present:
                raise VicUnavailable("The Airi CLI is not available on this server.")
            if authed:
                return {"state": "connected", "session_expires": self.session_expires}
            return {"state": "expired", "message": "The sign-in code expired. Try again for a new code."}
        with self._poll_lock:
            try:
                out = self._run(["auth", "poll", "-o", "json"], timeout=timeout_s)
            except subprocess.TimeoutExpired:
                return self._pending_out(p)
        data = _json(out.stdout)
        text = out.text.lower()
        if isinstance(data, dict) and data.get("authenticated") is True:
            with self._login_lock:
                self._pending = None
            return self._connected()
        if "authorization_pending" in text or "slow_down" in text:
            return self._pending_out(p)
        with self._login_lock:
            self._pending = None
        if "denied" in text:
            return {"state": "denied", "message": "The sign-in was denied in Airi. Try again."}
        if "expired" in text:
            return {"state": "expired", "message": "The sign-in code expired. Try again for a new code."}
        return {"state": "failed", "message": "Airi sign-in did not complete. Try again."}

    def logout(self) -> dict:
        with self._login_lock:
            self._pending = None
            out = self._run(["auth", "logout", "-o", "json"], timeout=20)
            self.probe(force=True)
            if out.returncode != 0 and self._probe_val[1]:
                raise VicError("AIRI_LOGOUT_FAILED", "The Airi CLI could not remove the session.")
            return {"state": "disconnected"}

    # -- readiness ----------------------------------------------------------
    def probe(self, *, force: bool = False) -> tuple[bool, bool]:
        """(cli_present, authenticated) from ``auth whoami``. Cached briefly."""
        now = time.monotonic()
        if not force and now - self._probe_at < self._PROBE_TTL:
            return self._probe_val
        try:
            out = self._run(["auth", "whoami", "-o", "json"], timeout=20)
            data = _json(out.stdout)
            if isinstance(data, dict) and "authenticated" in data:
                val = (True, data.get("authenticated") is True)
                exp = data.get("session_expires")
                self.session_expires = _safe(exp, 40) if val[1] and isinstance(exp, str) and exp else None
            else:
                val = (False, False)
        except (VicUnavailable, subprocess.TimeoutExpired):
            val = (False, False)
        if not val[1]:
            self.session_expires = None
        self._probe_val, self._probe_at = val, now
        self.cli_available = val[0]
        return val

    def ensure_ready(self) -> None:
        present, authed = self.probe(force=True)
        if not present:
            raise VicUnavailable("VIC_MODE=live but the Airi CLI is not available "
                                 "(npm i -g @airwallex/airi-cli@beta; check AIRI_CLI_COMMAND). "
                                 "Refusing to fall back to simulation.")
        if not authed:
            raise VicUnavailable("The Airi CLI is not signed in. Run `airi-cli auth login` and "
                                 "retry. Refusing to fall back to simulation.")

    def purchase(self, *, merchant: str, amount: Decimal, currency: str,
                 idempotency_key: str) -> VicPurchaseResult:
        raise VicUnavailable("Live VIC purchases are two-phase: create_mandate, then a human "
                             "authorizes in Airi, then complete.")

    # -- phase A ------------------------------------------------------------
    def create_mandate(self, *, merchant: str, merchant_url: str, amount: Decimal, currency: str,
                       item: str, description: str) -> MandateInfo:
        if self.test_card and amount > TEST_CARD_TX_LIMIT_USD:
            raise VicError("TX_LIMIT_EXCEEDED",
                           f"Test card limit is {money_str(TEST_CARD_TX_LIMIT_USD)} per transaction.")
        self.ensure_ready()

        pm = self._run(["payment-methods", "list", "-o", "json"])
        methods = _json(pm.stdout)
        if pm.returncode != 0 or not isinstance(methods, list):
            raise VicError("VIC_CLI_FAILED", f"Could not list Airi payment methods: {_safe(pm.text)}")
        cards = [m for m in methods if isinstance(m, dict) and m.get("paymentMethodId")
                 and m.get("type", "CARD") == "CARD"]
        if not cards:
            raise VicError("NO_PAYMENT_METHOD",
                           "No agent-enabled card in the Airi wallet. Add one at app.airi.com.")
        chosen = next((m for m in cards if m.get("asDefault")), cards[0])
        pm_id = str(chosen["paymentMethodId"])

        safe_item = re.sub(r"[,:]", " ", item).strip() or "Benefit"
        out = self._run([
            "mandate", "create", "-o", "json", "--source", AGENT_SOURCE,
            "--payment-method-id", pm_id,
            "--merchant-name", merchant, "--merchant-url", merchant_url,
            "--merchant-country", self.merchant_country,
            "--total-amount", money_str(amount), "--currency", currency.upper(),
            "--item", f"name:{safe_item},unit_price:{money_str(amount)},quantity:1",
            "--description", description])
        data = _json(out.stdout)
        if out.returncode != 0 or not isinstance(data, dict):
            raise VicError("VIC_CLI_FAILED", f"Airi could not create the mandate: {_safe(out.text)}")
        mandate_id = _first(data, "mandate_id", "mandateId")
        url = _first(data, "passkey_url", "passkeyUrl", "approval_url", "verification_url")
        if not mandate_id or not url or not url.lower().startswith("https://"):
            raise VicError("VIC_CLI_FAILED", "Airi returned an invalid mandate response.")
        return MandateInfo(mandate_id=mandate_id, approval_url=url,
                           payment_method_ref=f"airi_pm_{pm_id[:8]}")

    # -- phase B ------------------------------------------------------------
    def complete(self, *, mandate_id: str, merchant: str, amount: Decimal, currency: str,
                 executor: CheckoutExecutor, wait_seconds: int = 20) -> CompleteResult:
        self.ensure_ready()
        cred_path, cleanup = self._credential_path()
        try:
            out = self._run(["mandate", "get-credential", "-o", "json", "--mandate-id", mandate_id,
                             "--file", cred_path, "--timeout", str(wait_seconds)],
                            timeout=wait_seconds + self._GRACE)
            if out.returncode != 0:
                if "timed out" in out.text.lower():
                    return CompleteResult(state=AWAITING_AUTH)
                return CompleteResult(
                    state="FAILED", failure_code="VIC_CLI_FAILED",
                    failure_detail=f"Could not obtain the credential: {_safe(out.text)}")
            receipt = self._receipt(out.stdout)
            try:
                outcome = executor.execute(mandate_id=mandate_id, merchant=merchant, amount=amount,
                                           currency=currency, credential_file=cred_path)
            except Exception as exc:  # noqa: BLE001 - executor failures must still be reported
                logger.warning("VIC checkout executor failed: %s", type(exc).__name__)
                outcome = CheckoutOutcome("CANCELLED", "The checkout step failed.")
        except subprocess.TimeoutExpired:
            return CompleteResult(state=AWAITING_AUTH)
        finally:
            cleanup()

        reported = self._report(mandate_id, outcome)
        if outcome.result == "SUCCEEDED":
            return CompleteResult(state="COMPLETED", ok=True, receipt=receipt, result_reported=reported)
        if outcome.result == "DECLINED":
            return CompleteResult(state="DECLINED", receipt=receipt, result_reported=reported,
                                  failure_code="PROVIDER_DECLINED",
                                  failure_detail=outcome.reason or "The merchant declined the purchase.")
        return CompleteResult(state="CANCELLED", receipt=receipt, result_reported=reported,
                              failure_code="PURCHASE_NOT_CONFIRMED",
                              failure_detail=outcome.reason or "The purchase was not completed.")

    def _report(self, mandate_id: str, outcome: CheckoutOutcome) -> bool:
        args = ["mandate", "result", "-o", "json", "--mandate-id", mandate_id, "--result", outcome.result]
        if outcome.reason:
            args += ["--reason", _safe(outcome.reason, 200)]
        try:
            return self._run(args, timeout=30).returncode == 0
        except (VicUnavailable, subprocess.TimeoutExpired):
            return False

    def cancel_mandate(self, mandate_id: str) -> None:
        self.ensure_ready()
        out = self._run(["mandate", "cancel", "-o", "json", "--mandate-id", mandate_id])
        if out.returncode != 0:
            raise VicError("VIC_CLI_FAILED", f"Airi could not cancel the mandate: {_safe(out.text)}")

    # -- credential file ------------------------------------------------------
    def _credential_path(self):
        """A fresh private path for the CLI to write to, plus its cleanup.

        Under WSL the CLI writes inside Linux, so the path is a Linux /tmp path
        and cleanup runs ``rm -f`` through the same ``wsl ... --`` prefix.
        """
        stem = Path(self._command[0]).stem.lower()
        if stem == "wsl" and "--" in self._command:
            path = f"/tmp/paysilo-cred-{uuid.uuid4().hex}.json"
            prefix = self._command[: self._command.index("--") + 1]

            def cleanup() -> None:
                try:
                    subprocess.run([*prefix, "rm", "-f", path], capture_output=True, timeout=20,
                                   stdin=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    logger.warning("VIC: could not delete the WSL credential file.")
            return path, cleanup
        tmpdir = tempfile.mkdtemp(prefix="paysilo-vic-")
        os.chmod(tmpdir, 0o700)
        path = str(Path(tmpdir) / "credential.json")  # does not exist yet; CLI creates it 0600

        def cleanup() -> None:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return path, cleanup

    @staticmethod
    def _receipt(stdout: str) -> dict:
        data = _json(stdout)
        if not isinstance(data, dict):
            return {}
        return {k: _safe(str(data[k]), 60) for k in _RECEIPT_KEYS if k in data and data[k] is not None}


def build_vic_adapter(settings: Settings) -> VicAdapter:
    """``VIC_MODE=live`` → live Airi adapter; otherwise the simulated adapter."""
    if (settings.vic_mode or "").strip().lower() == "live":
        return AiriVicAdapter(settings)
    return SimulatedVicAdapter()


def vic_status(settings: Settings, adapter: VicAdapter) -> dict:
    """Public status for ``GET /benefits/status`` — mode is always explicit."""
    present, authed = adapter.probe()
    if adapter.mode == "SIMULATED":
        mode = "SIMULATED"
    elif present and authed:
        mode = "VIC"
    else:
        mode = "VIC_UNAVAILABLE"
    return {
        "mode": mode,
        "credential_ref": adapter.credential_ref,
        "test_card": adapter.test_card,
        "tx_limit_usd": money_str(TEST_CARD_TX_LIMIT_USD) if adapter.test_card else None,
        "cli_available": present,
        "authenticated": authed,
        "two_phase": adapter.two_phase,
        "session_expires": getattr(adapter, "session_expires", None) if authed else None,
    }
