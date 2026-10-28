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

from .tap import (
    TapAgentKey,
    TapAgentRegistry,
    TapReplayGuard,
    TapVerifyResult,
    generate_tap_agent,
    sign_request,
    verify_request,
)

AUTHORITY = "agent.paysilo.app"


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
