# Platform fee path: `/charges/create` (decision)

## Decision

PaySilo collects its per-run platform fee with **`POST /charges/create`** —
one charge, against the single employer whose payroll ran, for exactly
USD 149, created by the graph's fee step *after* every payout has been sent
and with the deterministic request id `request_id_for(run_id, "platform-fee")`.

`application_fee_options` on transfers is **not** used.

## Why

The fee is a property of the **run**, not of any individual payout:

- **Once per run, only on success.** The fee step runs only after the whole
  payroll executed (status `SENT`), and never for a held, refused or rejected
  run — Borealis is short and pays no fee. An application fee rides on each
  transfer at creation time: it would be collected from the first payout even
  if a later payout failed, and would need clawing back on partial failure.
- **Flat amount, no splitting.** USD 149 does not divide across a variable
  number of payouts, currencies and methods. Splitting it per transfer would
  invent rounding remainder rules and couple the fee to payout composition.
  A single charge carries the exact amount the policy priced.
- **Tenant isolation.** A charge names one `source_id` — the employer's own
  connected account — and the client sends it with `x-on-behalf-of` set to
  that same account, so the debit can only ever leave that employer's silo.
  It is priced into that employer's affordability check up front (payroll +
  payout fees + USD 149 run fee + USD 1,000 reserve floor), so a settled run
  can never be surprised by it.
- **Idempotency and recovery.** The charge reuses the run's deterministic
  `request_id`, so a retry returns the original charge instead of debiting
  twice. Airwallex reports a reused charge `request_id` with the error code
  `request_id_duplicate` and `details.source_id` naming the original charge;
  the client follows that id and returns the original resource.
- **Ledger symmetry.** One charge maps to exactly one ledger posting
  (employer cash −149 / platform fees +149) and one reconciliation figure.
  Per-transfer application fees would surface inside transfer responses in
  mixed currencies and complicate the proof that the books balance.

## What `application_fee_options` would be for

It is the right tool for a platform that takes a percentage or a small fixed
cut *of each transaction* (for example a marketplace taxing every payout).
PaySilo's commercial model is a flat per-run fee decided by deterministic
policy before approval, so the fee belongs on its own resource, not smeared
across transfers.

## Implementation

- Client: `AirwallexClient.create_charge` (`POST /charges/create`,
  `x-on-behalf-of: <source account>`, duplicate recovery via
  `details.source_id` → `GET /charges/{id}`).
- Simulator: `AirwallexSimulator.create_charge` implements the same
  contract (debit the source employer, credit the platform wallet, return
  the original result on a repeated `request_id`), so demo mode and the
  graph's fee step behave identically in both modes.
