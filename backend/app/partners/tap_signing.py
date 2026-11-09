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
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

AUTHORITY = "agent.paysilo.app"


def signer_from_key_material(agent_id: str, private_key_material: str,
                             key_id: str | None = None) -> "TapSigner":
    """A signer backed by a persistent key (``TAP_PRIVATE_KEY``)."""
    return TapSigner(key=load_tap_agent(agent_id, private_key_material, key_id=key_id))


class StoredApproval(Protocol):
    """The fields of an ``approvals`` row needed to re-verify it."""

    run_id: object
    signed_body: str | None
    tap_signature_input: str | None
    tap_signature: str | None
    created_at: datetime | None


def registry_from_tap_key_rows(rows) -> TapAgentRegistry:
    """Build a verification registry from ``tap_keys`` rows (active + retired)."""
    registry = TapAgentRegistry()
    for row in rows:
        registry.register_pem(key_id=row.key_id, agent_id=row.agent_id, pem=row.public_key_pem)
    return registry


def verify_stored_approval(approval: StoredApproval, registry: TapAgentRegistry) -> TapVerifyResult:
    """Re-verify a stored approval signature against published public keys.

    This is a read-only audit check: it does not consume the nonce (replay
    protection is enforced once, at execution time, by the graph and by the
    unique ``tap_nonce`` column). Freshness is evaluated at the approval's
    own ``created_at`` — TAP signatures are short-lived request signatures,
    so the meaningful question for a stored approval is whether it was
    valid when it was presented, not whether its 5-minute window is still
    open today. Tampering with the stored body still fails the digest /
    signature checks exactly as a live request would.
    """
    if not approval.signed_body or not approval.tap_signature_input or not approval.tap_signature:
        return TapVerifyResult(False, "missing_signature",
                               detail="This approval row carries no TAP signature to verify.")
    headers = {
        "Signature-Input": approval.tap_signature_input,
        "Signature": approval.tap_signature,
        # The Content-Digest header was not stored separately; it is a pure
        # function of the stored signed body, so it is recomputed here and
        # then checked by verify_request like any covered header.
        "Content-Digest": content_digest(approval.signed_body),
    }
    at: int | None = None
    if approval.created_at is not None:
        created = approval.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        at = int(created.timestamp())
    return verify_request(
        method="POST", authority=AUTHORITY, path=f"/runs/{approval.run_id}/decision",
        headers=headers, registry=registry, replay_guard=None,
        body=approval.signed_body, now_seconds=at,
    )


def canonical_json(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def approval_body(*, run_id: str, employer_key: str, decision: str, assessment: dict,
                  pay_lines: list[dict], decided_by: str) -> dict:
    """The exact thing a human approves. Changing any field breaks the signature."""
    return {
        "type": "payroll.approval",
        "run_id": run_id,
        "employer": employer_key,
        "decision": decision,
        "decided_by": decided_by,
        "required_usd": assessment.get("required_usd"),
        "total_required": assessment.get("total_required"),
        "payroll_currency": assessment.get("payroll_currency"),
        "pay_lines": [
            {"name": l["name"], "amount": l["amount"], "currency": l["currency"],
             "method": l.get("method", "SWIFT")}
            for l in pay_lines
        ],
    }


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
