"""Visa TAP signing for PaySilo's two money-critical messages.

1. **Approvals** — when a human approves (or holds) a payroll, the decision
   is signed over a canonical body that pins the run, employer, amounts,
   currencies and every pay line. The graph verifies that signature before
   any money moves, so an approval can't be replayed, edited or redirected.
2. **Payout instructions** — each transfer instruction is signed by the
   PaySilo agent key and verified immediately before ``create_transfer``.

Signatures are RFC 9421-style (``app.partners.tap``): Ed25519 over method,
authority, path and a SHA-256 Content-Digest of the body, with a nonce
replay guard and a short expiry window.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .tap import (
    TapAgentKey,
    TapAgentRegistry,
    TapReplayGuard,
    TapVerifyResult,
    content_digest,
    generate_tap_agent,
    load_tap_agent,
    sign_request,
    verify_request,
)

logger = logging.getLogger(__name__)

AUTHORITY = "agent.paysilo.app"


class TapKeyError(RuntimeError):
    """TAP key configuration or lifecycle problem (never includes key material)."""


def signer_from_key_material(agent_id: str, private_key_material: str,
                             key_id: str | None = None) -> "TapSigner":
    """A signer backed by a persistent key (``TAP_PRIVATE_KEY``)."""
    return TapSigner(key=load_tap_agent(agent_id, private_key_material, key_id=key_id))


def signer_for_settings(settings) -> tuple["TapSigner", bool]:
    """Build the process signer. Returns ``(signer, persistent)``.

    Live mode (``settings.demo_mode`` false) REFUSES to sign with an
    ephemeral key: approvals must stay verifiable across restarts, so
    ``TAP_PRIVATE_KEY`` is mandatory. Demo mode may use a throwaway key.
    """
    if settings.tap_private_key:
        try:
            return signer_from_key_material(settings.tap_agent_id, settings.tap_private_key,
                                            key_id=settings.tap_key_id), True
        except ValueError as exc:
            raise TapKeyError(f"TAP_PRIVATE_KEY is not usable: {exc}") from None
    if not settings.demo_mode:
        raise TapKeyError(
            "TAP_PRIVATE_KEY is required in live mode: refusing to sign money approvals with an "
            "ephemeral key that would not verify after a restart.")
    import uuid
    key = generate_tap_agent(settings.tap_agent_id,
                             key_id=f"{settings.tap_agent_id}-{uuid.uuid4().hex[:12]}")
    return TapSigner(key=key), False


def register_active_key(session, key: TapAgentKey, *, now: datetime | None = None):
    """Persist ``key`` as THE active key in ``tap_keys``.

    - Reusing a ``key_id`` with a different public key raises
      :class:`TapKeyError` (never silently overwritten).
    - Every other key that is still active is marked ``retired_at`` (it
      stays published/verifiable, but no longer signs).
    Caller commits.
    """
    from sqlalchemy import select

    from ..db.models import TapKey

    now = now or datetime.now(UTC)
    row = session.get(TapKey, key.key_id)
    pem = key.public_key_pem()
    if row is not None:
        if row.public_key_pem.strip() != pem.strip():
            raise TapKeyError(
                f"TAP key id {key.key_id!r} is already registered with a different public key; "
                "refusing to overwrite. Use a new TAP_KEY_ID for a new key.")
        if row.revoked_at is not None:
            raise TapKeyError(f"TAP key id {key.key_id!r} was revoked and cannot sign again.")
        row.retired_at = None
    else:
        row = TapKey(key_id=key.key_id, agent_id=key.agent_id, public_key_pem=pem, algorithm="ed25519")
        session.add(row)
    session.flush()
    for other in session.scalars(select(TapKey).where(TapKey.key_id != key.key_id,
                                                      TapKey.retired_at.is_(None))):
        other.retired_at = now
    return row


class StoredApproval(Protocol):
    """The fields of an ``approvals`` row needed to re-verify it."""

    run_id: object
    signed_body: str | None
    tap_signature_input: str | None
    tap_signature: str | None
    created_at: datetime | None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def registry_from_tap_key_rows(rows, *, now: datetime | None = None) -> TapAgentRegistry:
    """Build a verification registry from ``tap_keys`` rows (active + retired).

    Revoked and expired keys are NOT registered, so signatures under them
    stop verifying.
    """
    now = now or datetime.now(UTC)
    registry = TapAgentRegistry()
    for row in rows:
        if getattr(row, "revoked_at", None) is not None:
            continue
        expires = _aware(getattr(row, "expires_at", None))
        if expires is not None and expires <= now:
            continue
        try:
            registry.register_pem(key_id=row.key_id, agent_id=row.agent_id, pem=row.public_key_pem)
        except Exception:  # noqa: BLE001 - a corrupt row must not stop boot; it just can't verify
            logger.warning("Visa TAP: tap_keys row %s has an unusable public key; skipped.", row.key_id)
    return registry


def verify_stored_approval(approval: StoredApproval, registry: TapAgentRegistry) -> TapVerifyResult:
    """Re-verify a stored approval signature against published public keys.

    Read-only audit check: it does not consume the nonce (replay protection
    is enforced once, at execution time, by the graph and the unique
    ``tap_nonce`` column). Freshness is evaluated at the signature's OWN
    ``created`` time (from ``Signature-Input``), not the DB insert time and
    not today: the question is whether the signature was well-formed and
    within its declared window when it was made. Tampering with the stored
    body still fails the digest / signature checks.
    """
    if not approval.signed_body or not approval.tap_signature_input or not approval.tap_signature:
        return TapVerifyResult(False, "missing_signature",
                               detail="This approval row carries no TAP signature to verify.")
    headers = {
        "Signature-Input": approval.tap_signature_input,
        "Signature": approval.tap_signature,
        # Content-Digest is a pure function of the stored body; recompute it
        # and let verify_request check it like any covered header.
        "Content-Digest": content_digest(approval.signed_body),
    }
    m = re.search(r"created=(\d+)", approval.tap_signature_input)
    at = int(m.group(1)) if m else None
    return verify_request(
        method="POST", authority=AUTHORITY, path=f"/runs/{approval.run_id}/decision",
        headers=headers, registry=registry, replay_guard=None,
        body=approval.signed_body, now_seconds=at,
    )


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _same(a: object, b: object) -> bool:
    da, db = _dec(a), _dec(b)
    if da is not None and db is not None:
        return da == db
    return a == b


def compare_signed_body_to_run(signed_body: str, *, run_id: str, assessment: dict | None,
                               pay_lines: list[dict], held_lines: list[dict] | None = None) -> str | None:
    """Check that what the human signed is what the run actually holds.

    Returns ``None`` when consistent, else a short mismatch description.
    ``pay_lines`` are ``{"name","amount","currency","method"}`` dicts in
    run order.
    """
    try:
        body = json.loads(signed_body)
    except ValueError:
        return "signed body is not valid JSON"
    if str(body.get("run_id")) != str(run_id):
        return "signed run_id differs from the run"
    for field_name in ("required_usd", "total_required", "payroll_currency"):
        if assessment is None or field_name not in assessment:
            return f"run has no stored assessment value for {field_name}"
        if not _same(body.get(field_name), assessment[field_name]):
            return f"signed {field_name} differs from the run's assessment"
    signed_lines = body.get("pay_lines") or []
    if len(signed_lines) != len(pay_lines):
        return "number of signed pay lines differs from the run"
    for i, (signed, actual) in enumerate(zip(signed_lines, pay_lines)):
        if (signed.get("name") != actual["name"] or signed.get("currency") != actual["currency"]
                or (signed.get("method") or "SWIFT") != (actual.get("method") or "SWIFT")
                or not _same(signed.get("amount"), actual["amount"])):
            return f"signed pay line {i + 1} differs from the run"
    signed_held = body.get("held_lines") or []
    held_lines = held_lines or []
    if len(signed_held) != len(held_lines):
        return "number of signed held lines differs from the run"
    for i, (signed, actual) in enumerate(zip(signed_held, held_lines)):
        if (signed.get("name") != actual["name"] or signed.get("currency") != actual["currency"]
                or (signed.get("method") or "SWIFT") != (actual.get("method") or "SWIFT")
                or not _same(signed.get("amount"), actual["amount"])):
            return f"signed held line {i + 1} differs from the run"
    return None


def canonical_json(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def _line_fields(lines: list[dict]) -> list[dict]:
    return [{"name": l["name"], "amount": l["amount"], "currency": l["currency"],
             "method": l.get("method") or "SWIFT"} for l in lines]


def approval_body(*, run_id: str, employer_key: str, decision: str, assessment: dict,
                  pay_lines: list[dict], decided_by: str,
                  held_lines: list[dict] | None = None) -> dict:
    """The exact thing a human approves. Changing any field breaks the signature.

    ``held_lines`` (partial payout) are the lines this run deliberately does NOT
    pay. They are bound too, so a signature over a different split is rejected.
    The key is omitted when nothing is held (full payouts keep their old shape).
    """
    body = {
        "type": "payroll.approval",
        "run_id": run_id,
        "employer": employer_key,
        "decision": decision,
        "decided_by": decided_by,
        "required_usd": assessment.get("required_usd"),
        "total_required": assessment.get("total_required"),
        "payroll_currency": assessment.get("payroll_currency"),
        "pay_lines": _line_fields(pay_lines),
    }
    if held_lines:
        body["held_lines"] = _line_fields(held_lines)
    return body


def payout_body(*, run_id: str, employer_key: str, account_id: str, line: dict, index: int) -> dict:
    return {
        "type": "payroll.payout",
        "run_id": run_id,
        "employer": employer_key,
        "on_behalf_of": account_id,
        "index": index,
        "beneficiary": line["name"],
        "amount": line["amount"],
        "currency": line["currency"],
        "method": line.get("method", "SWIFT"),
    }


@dataclass
class SignedEnvelope:
    path: str
    body: str  # canonical JSON
    headers: dict[str, str]

    def as_dict(self) -> dict:
        return {"path": self.path, "body": self.body, "headers": dict(self.headers)}

    @classmethod
    def from_dict(cls, data: dict) -> "SignedEnvelope":
        return cls(path=str(data["path"]), body=str(data["body"]), headers=dict(data["headers"]))


@dataclass
class TapSigner:
    """Holds the agent key, the registry that knows it, and the replay guard."""

    key: TapAgentKey = field(default_factory=lambda: generate_tap_agent("paysilo-agent"))
    registry: TapAgentRegistry = field(default_factory=TapAgentRegistry)
    replay_guard: TapReplayGuard = field(default_factory=TapReplayGuard)

    def __post_init__(self) -> None:
        self.registry.register(self.key)

    def sign(self, path: str, body: dict) -> SignedEnvelope:
        text = canonical_json(body)
        headers = sign_request(self.key, method="POST", authority=AUTHORITY, path=path, body=text)
        return SignedEnvelope(path=path, body=text, headers=headers)

    def verify(self, envelope: SignedEnvelope, expected_body: dict) -> TapVerifyResult:
        """Verify the signature AND that it covers exactly ``expected_body``."""
        if envelope.body != canonical_json(expected_body):
            return TapVerifyResult(False, "body_mismatch",
                                   detail="Signed body does not match the payroll being executed.")
        return verify_request(method="POST", authority=AUTHORITY, path=envelope.path,
                              headers=envelope.headers, registry=self.registry,
                              replay_guard=self.replay_guard, body=envelope.body)

    def sign_approval(self, **kwargs) -> SignedEnvelope:
        body = approval_body(**kwargs)
        return self.sign(f"/runs/{body['run_id']}/decision", body)

    def sign_payout(self, **kwargs) -> SignedEnvelope:
        body = payout_body(**kwargs)
        return self.sign(f"/runs/{body['run_id']}/payouts/{body['index']}", body)
