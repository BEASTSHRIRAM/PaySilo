# TAP-style signing — PaySilo implementation notes

**Status / claim discipline:** TAP is not available as a sponsor tool in
this hackathon, and this module is not a Visa product integration. It is
PaySilo's internal approval-integrity mechanism, implemented following the
TAP signature scheme from Visa's published sample. PaySilo's Visa
integration is VIC via Airi (`app/partners/vic.py`).

PaySilo signs two money-critical messages with Trusted Agent Protocol
(TAP)-style signatures (`app/partners/tap.py`, `app/partners/tap_signing.py`):

1. **Human approvals** — a decision is signed over a canonical body pinning
   the run, employer silo, amounts, currencies and every pay line.
2. **Payout instructions** — each transfer instruction is signed by the
   agent key and verified immediately before the transfer is created.

Money moves only on a signature that verifies. A captured approval can
never pay twice or be redirected to another employer.

## Signing key management

- Set `TAP_PRIVATE_KEY` in the environment (PEM text, base64-encoded PEM,
  or a base64 raw 32-byte Ed25519 seed). The same key — and a stable keyid —
  is then used in every process, so signatures survive restarts.
- With `TAP_PRIVATE_KEY` unset, a fresh **ephemeral dev key** is generated
  per process and logged at warning level as ephemeral. That is demo-only
  behaviour: approvals signed by a previous process stop verifying.
- On startup the active public key is registered in the `tap_keys` table
  (`keyid`, `agent_id`, `public_key_pem`, `algorithm`, `created_at`,
  `retired_at`, `expires_at`, `revoked_at`), and every non-revoked key in
  that table is loaded into the verification registry.

## Published keys and verification

- `GET /.well-known/tap-keys` (public) returns all non-expired,
  non-revoked public keys — active and retired-but-verifiable — as
  `{keys: [{keyid, agent_id, algorithm, status, public_key, public_key_pem,
  created_at, retired_at, expires_at}]}`. `public_key` is the raw 32-byte
  Ed25519 key, base64-encoded.
- `POST /tap/verify` (authenticated, tenant-scoped) takes
  `{approval_id}` or `{run_id}`, loads the stored approval from the
  `approvals` table, rebuilds the verification registry from `tap_keys`,
  and returns `{verified, keyid, agent_id, code, reason, ...}`. Reason
  codes are the verifier's: `ok`, `digest_mismatch`, `bad_signature`,
  `expired`, `replayed`, `unregistered_agent`, `missing_signature`, etc.
  Another organization's approval is a 404, never a result.

## Key rotation

1. Generate a new Ed25519 key and set `TAP_PRIVATE_KEY` to it (optionally
   pin `TAP_KEY_ID`; otherwise a new keyid is derived from the new public
   key).
2. Restart. The new key registers itself as active; the previous key's row
   stays in `tap_keys` (mark it `retired_at` to label it retired in the
   published list).
3. New signatures use the new keyid; old signatures keep verifying because
   verification accepts every published key. This is covered by
   `tests/test_tap.py::test_key_rotation_old_signatures_still_verify`.

## Spec alignment

- **Signature scheme:** RFC 9421 HTTP Message Signatures with Ed25519
  (`alg="ed25519"`). Covered components: `@method`, `@authority`, `@path`,
  plus `content-digest` whenever a body is signed.
- **Body binding:** `Content-Digest: sha-256=:<base64>:` over the exact
  canonical-JSON body; a tampered body fails with `digest_mismatch`.
- **Tag:** every signature carries `tag="agent-payer-auth"` (the TAP
  payment-authorisation tag), matching Visa TAP's use of a signature tag
  to declare the signature's purpose.
- **Parameters:** `created` / `expires` (default 300 s window, 60 s clock
  skew), a single-use `nonce` (replay guard in-process, unique
  `approvals.tap_nonce` column in the database), and `keyid`.
- **Keyid format:** `paysilo-agent-<first 16 hex of sha256(raw public
  key)>` when derived (stable per key, safe to publish); an explicit
  `TAP_KEY_ID` or the dev key's random suffix may be used instead. The
  agent id is `paysilo-agent` (`TAP_AGENT_ID`).

## Deviations (explicit)

- **Registry:** Visa TAP resolves keys through Visa's agent registry /
  Web Bot Auth-style key directory. PaySilo publishes the same data itself
  at `/.well-known/tap-keys` from its own `tap_keys` table; registering
  with Visa's live registry requires Visa-issued credentials and is out of
  scope for the demo. The key document format is PaySilo's own clean JSON,
  not a JWK Set.
- **Authority binding:** signatures bind the logical PaySilo authority
  `agent.paysilo.app`, not the public HTTP host of a given deployment.
- **Stored-approval verification is historical:** `POST /tap/verify`
  evaluates the freshness window at the approval's `created_at` (when the
  signature was presented) and does not consume the nonce again — replay
  protection is enforced once at execution time. Cryptographic and digest
  checks are identical to live verification.
