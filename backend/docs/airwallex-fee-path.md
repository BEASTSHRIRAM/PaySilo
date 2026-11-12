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
- **Tenant isolation.** A charge names exactly one `source` — the employer's own
  connected account (Kit 6 shape: `source` is a plain `acct_...` string, the
  `reason` is the enum value `professional_business_services`, and the call
  is sent WITHOUT `x-on-behalf-of`, because the platform is the beneficiary).
  The debit can only ever leave that one employer's silo.
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
  no `x-on-behalf-of`, `source: <employer account>`, duplicate recovery via
  `details.source_id` → `GET /charges/{id}`).
- Simulator: `AirwallexSimulator.create_charge` implements the same
  contract (debit the source employer, credit the platform wallet, return
  the original result on a repeated `request_id`), so demo mode and the
  graph's fee step behave identically in both modes.


## Fixture provenance (which recorded shapes are doc-verified)

`backend/tests/fixtures/airwallex/*.json` mix two kinds of evidence. Checked
on 2026-11-09 against the Kit 6 guide (airwallexdev.com/guide) and the API
reference pages for Accounts and FX Conversion:

Doc-verified (request side): `POST /accounts/create`, `/accounts/{id}/update`
(EIN in `account_details.business_details.business_identifiers`, entries
`{country_code, number, type: "EIN"}`), `/accounts/{id}/submit` (empty body),
`/simulation/accounts/{id}/update_status` (`{"next_status":"ACTIVE","force":true}`),
`POST /charges/create` (`source`, `reason`, `reference` required; status `NEW`
until `SETTLED`), `POST /fx/conversions/create` (`buy_currency`, `sell_currency`,
one of `buy_amount`/`sell_amount`, `request_id`; settles immediately;
no `x-api-version`).

Assumed / self-authored (response bodies and anything not named above):
`auth_login`, `balances_current`, `beneficiary_*`, `*_duplicate_*`,
`connected_account_*`, `deposit_*`, `fx_*`, `hosted_flow_*`,
`simulation_deposit_create`, `transfer_*`. They follow documented field names
but were not captured from a live sandbox. `term_agreement` on FX conversions
is NOT in the published reference for conversions create, so it is not sent.
Not verified: the exact `/accounts/{id}/update` body (we reuse the create
`account_details` shape) and whether `/accounts/create` honours `request_id`
(we do not send one; the graph persists the account id per employer).

### Update: live-sandbox verification (2026-11-09/10)

Two fresh employer accounts and one reused account were driven end to end
against `api.sandbox.airwallex.com` with this client. **Live-verified shapes**
(fixtures are now sanitised copies of real responses, ids/amounts synthetic):
`balances_current` (bare JSON array), `simulation_deposit_create` (PENDING,
already in `available_amount`), `global_account_create`, `fx_quote_create`
(`quote_id`, `validity` required, we send `MIN_1`), `fx_conversion_create`
(`conversion_id`, SETTLED), `beneficiary_create` / `beneficiary_validate`
(`"OK"`; `beneficiary.address.{country_code,street_address,city,postcode}`
required for SWIFT), `transfer_*` (`reference` required; SCHEDULED/PROCESSING,
then `next_status` SENT then PAID; PROCESSING -> PAID directly is HTTP 500),
`charge_*` (NEW, then PENDING/SETTLED; `fee: 0`), `connected_account_*`, and
the full activation payload (see `create_connected_account`).
Also live: a reused conversion `request_id` is HTTP 400 `invalid_argument`
(handled as a duplicate); `/simulation/deposit/create` IGNORES `request_id`
(not idempotent, never retried by the client); the SWIFT payout fee is
EUR 13.38-13.39 on the sandbox (rate-dependent), not a flat 12.85, so
`policy.py`'s 12.85 under-prices it. Still self-authored: `auth_login`,
`hosted_flow_*`, `*_duplicate_409` bodies. Not observed:
`unconfigured_client_fee` (never returned in these runs).
