"""Visa Trusted Access Protocol (TAP) style request signing.

Implements RFC 9421-style
HTTP message signatures with Ed25519, a SHA-256 ``Content-Digest`` binding
the request body, an agent registry, a nonce replay guard, and a
created/expires freshness window.

PaySilo's job for TAP: every payout instruction and every human approval
is signed, bound to the exact employer / amount / currency / beneficiary,
single-use and replay-proof — a captured approval can never pay twice or
be redirected to another employer.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

TAP_TAG = "agent-payer-auth"
DEFAULT_TTL_SECONDS = 300
DEFAULT_SKEW_SECONDS = 60


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@dataclass
class TapAgentKey:
    agent_id: str
    key_id: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")


def generate_tap_agent(agent_id: str, key_id: str | None = None) -> TapAgentKey:
    private = Ed25519PrivateKey.generate()
    return TapAgentKey(
        agent_id=agent_id,
        key_id=key_id or f"{agent_id}-key-1",
        private_key=private,
        public_key=private.public_key(),
    )


def content_digest(body: str) -> str:
    """RFC 9421 ``Content-Digest`` value binding a request body (sha-256)."""
    return f"sha-256=:{_b64(hashlib.sha256(body.encode('utf-8')).digest())}:"


@dataclass
class TapAgentRegistry:
    """keyId -> public key, standing in for Visa's Agent Registry."""

    _agents: dict[str, tuple[str, Ed25519PublicKey]] = field(default_factory=dict)

    def register(self, key: TapAgentKey) -> None:
        self._agents[key.key_id] = (key.agent_id, key.public_key)

    def lookup(self, key_id: str) -> tuple[str, Ed25519PublicKey] | None:
        return self._agents.get(key_id)


@dataclass
class TapReplayGuard:
    """Nonces are single-use; a captured request cannot be replayed."""

    _seen: set[str] = field(default_factory=set)

    def record(self, nonce: str) -> bool:
        """Returns False when the nonce was already used."""
        if nonce in self._seen:
            return False
        self._seen.add(nonce)
        return True


def _signature_base(method: str, authority: str, path: str, components: list[str],
                    params_value: str, header_values: dict[str, str]) -> str:
    lines: list[str] = []
    for component in components:
        if component == "@method":
            lines.append(f'"@method": {method.upper()}')
        elif component == "@authority":
            lines.append(f'"@authority": {authority}')
        elif component == "@path":
            lines.append(f'"@path": {path}')
        else:
            lines.append(f'"{component}": {header_values.get(component, "")}')
    lines.append(f'"@signature-params": {params_value}')
    return "\n".join(lines)


def sign_request(key: TapAgentKey, *, method: str, authority: str, path: str,
                 body: str | None = None, tag: str = TAP_TAG,
                 created: int | None = None, expires: int | None = None,
                 nonce: str | None = None) -> dict[str, str]:
    """Produce ``Signature-Input`` / ``Signature`` (+ ``Content-Digest``) headers."""
    created = created if created is not None else int(time.time())
    expires = expires if expires is not None else created + DEFAULT_TTL_SECONDS
    nonce = nonce or str(uuid.uuid4())
    components = ["@method", "@authority", "@path"]
    headers: dict[str, str] = {}
    header_values: dict[str, str] = {}
    if body is not None:
        digest = content_digest(body)
        headers["Content-Digest"] = digest
        header_values["content-digest"] = digest
        components.append("content-digest")
    params_value = (
        "(" + " ".join(f'"{c}"' for c in components) + ");"
        f'created={created};keyid="{key.key_id}";alg="ed25519";'
        f'expires={expires};nonce="{nonce}";tag="{tag}"'
    )
    base = _signature_base(method, authority, path, components, params_value, header_values)
    signature = key.private_key.sign(base.encode("utf-8"))
    headers["Signature-Input"] = f"sig1={params_value}"
    headers["Signature"] = f"sig1=:{_b64(signature)}:"
    return headers


@dataclass
class TapVerifyResult:
    valid: bool
    code: str = "ok"
    agent_id: str | None = None
    nonce: str | None = None
    detail: str = ""


_PARAMS_RE = re.compile(r"^sig1=\(([^)]*)\)(;.*)$")


def _parse_signature_input(value: str) -> tuple[list[str], dict[str, str]] | None:
    m = _PARAMS_RE.match(value)
    if not m:
        return None
    components = re.findall(r'"([^"]+)"', m.group(1))
    params: dict[str, str] = {}
    for raw in m.group(2).split(";"):
        raw = raw.strip()
        if not raw or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        params[k.strip()] = v.strip().strip('"')
    return components, params


def verify_request(*, method: str, authority: str, path: str, headers: dict[str, str],
                   registry: TapAgentRegistry, replay_guard: TapReplayGuard | None = None,
                   body: str | None = None, now_seconds: int | None = None,
                   skew_seconds: int = DEFAULT_SKEW_SECONDS) -> TapVerifyResult:
    """Verify a TAP-signed request: shape, registry, freshness, digest,
    signature, replay — in that order."""
    lower = {k.lower(): v for k, v in headers.items()}
    sig_input = lower.get("signature-input")
    sig_header = lower.get("signature")
    if not sig_input or not sig_header:
        return TapVerifyResult(False, "missing_headers",
                               detail="Signature-Input and Signature headers are both required.")
    parsed = _parse_signature_input(sig_input)
    if parsed is None:
        return TapVerifyResult(False, "malformed_signature_input", detail="Signature-Input did not parse.")
    components, params = parsed
    for required in ("@method", "@authority", "@path"):
        if required not in components:
            return TapVerifyResult(False, "malformed_signature_input",
                                   detail=f"Covered components must include {required}.")

    key_id, nonce = params.get("keyid"), params.get("nonce")
    if not key_id or not nonce:
        return TapVerifyResult(False, "malformed_signature_input", detail="keyid and nonce are required.")
    registered = registry.lookup(key_id)
    if registered is None:
        return TapVerifyResult(False, "unregistered_agent", detail=f"No registry entry for keyId {key_id}.")
    agent_id, public_key = registered
    if params.get("alg", "ed25519") != "ed25519":
        return TapVerifyResult(False, "unsupported_algorithm", detail="Only ed25519 is supported here.")

    now = now_seconds if now_seconds is not None else int(time.time())
    try:
        created, expires = int(params.get("created", "0")), int(params.get("expires", "0"))
    except ValueError:
        return TapVerifyResult(False, "malformed_signature_input", detail="created/expires must be integers.")
    if created - skew_seconds > now:
        return TapVerifyResult(False, "not_yet_valid", detail=f"Signature created at {created} is in the future.")
    if expires + skew_seconds < now:
        return TapVerifyResult(False, "expired", detail=f"Signature expired at {expires}.")

    m = re.match(r"^sig1=:([^:]+):$", sig_header)
    if not m:
        return TapVerifyResult(False, "malformed_signature_input", detail="Signature header did not parse.")
    signature_b64 = m.group(1)

    header_values: dict[str, str] = {}
    for component in components:
        if component.startswith("@"):
            continue
        value = lower.get(component)
        if value is None:
            return TapVerifyResult(False, "missing_headers",
                                   detail=f'Covered component "{component}" has no header.')
        header_values[component] = value
    if "content-digest" in components and body is not None:
        if header_values["content-digest"] != content_digest(body):
            return TapVerifyResult(False, "digest_mismatch",
                                   detail="The request body does not match the covered Content-Digest.")

    params_value = sig_input[len("sig1="):]
    base = _signature_base(method, authority, path, components, params_value, header_values)
    try:
        public_key.verify(base64.b64decode(signature_b64), base.encode("utf-8"))
    except Exception:
        return TapVerifyResult(False, "bad_signature", detail="Ed25519 verification failed.")

    if replay_guard is not None and not replay_guard.record(nonce):
        return TapVerifyResult(False, "replayed", detail=f"Nonce {nonce} was already used; replay refused.")

    return TapVerifyResult(True, "ok", agent_id=agent_id, nonce=nonce)
