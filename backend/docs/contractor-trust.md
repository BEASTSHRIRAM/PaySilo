# Contractor trust: onboarding, tax tracking, bank-change control

PaySilo is not tax software. It stores **statuses and dates only** (never tax ids or documents) and every
compliance screen says "not tax advice".

## Bank-detail change control (four-eyes)

Fake "we changed our bank details" requests are a leading payment-fraud vector, so any change to
`iban`, `swift_code`, `account_number`, `routing_code`, `payout_method` or `currency` (via
`PATCH /employers/{eid}/contractors/{cid}`, or a submission through the onboarding link) sets:

| column | value |
| --- | --- |
| `bank_verification_status` | `PENDING_VERIFICATION` |
| `bank_details_changed_at` | now |
| `bank_change_requested_by_id` | the editing user (NULL when the contractor submitted via the link) |

`POST .../verify-bank` (owner/admin/approver) requires `{"callback_confirmed": true}` ("I confirmed this change
with the contractor through a known, independent channel"), a DIFFERENT user than `bank_change_requested_by_id`
(403 otherwise; any permitted user may verify when the requester is NULL), and a passing validate-only check
(`AirwallexClient.validate_beneficiary` -> `/beneficiaries/validate`, creates nothing). It sets `VERIFIED`,
`bank_verified_by_id`, `bank_verified_at` and writes a log row. `ContractorChange.summary` is masked (last 4 only).

### Contract for payout gating / risk rules (wired by another agent)

* **Do not pay** a contractor whose `bank_verification_status != "VERIFIED"`; surface it as an exception
  ("bank details changed on <bank_details_changed_at>, awaiting second-person verification").
* Existing rows migrate as `VERIFIED` (server default), so nothing is blocked retroactively.
* `bank_details_changed_at` is available for "recent bank change before a large payment" risk rules.
* The onboarding portal can never make details payable; only `verify-bank` flips the status.

## Self-serve onboarding

`POST .../onboarding-link` (owner/admin) returns `invite_path` (`/onboard/<token>`) once; only `sha256(token)` is
stored, links expire after 14 days, a new link revokes the previous live one, and `.../onboarding-link/{id}/revoke`
revokes explicitly. Public endpoints: `GET /onboarding/{token}` and `POST /onboarding/{token}/submit`. Unknown,
expired, revoked and used tokens all return the same 404; both endpoints share a per-IP throttle (20 failures / 5 min).
Payout options offered: EUR SWIFT, EUR SEPA, USD ACH, GBP Faster Payments. The submission carries a US-person /
non-US self-declaration (no tax ids, no documents).

## Tax tracking and the 1099 watch

Per contractor: `tax_status` (US_PERSON | NON_US | UNKNOWN), `tax_form_type` (W9 | W8BEN | W8BENE | NONE),
`tax_form_status` (MISSING | RECEIVED | EXPIRED), received / expiry dates; staff-attested. A RECEIVED form with a past
expiry reads as EXPIRED (computed on read). Forms expiring within 60 days are flagged.

`GET /compliance/overview` (any member): needs-attention lists plus per-contractor calendar-year-to-date paid.
**YTD derivation (persisted data only):** for every PAID `PayLine` of a run settled (else created) in the current
calendar year, USD = `amount * (required_usd / required)` using that run's stored `assessment.by_currency` row for the
line's currency (USD = 1). A line with no stored rate is not guessed: it is counted in `unpriced_lines`.
The 1099 watch lists US-person contractors with YTD >= `TAX_1099_THRESHOLD_USD` (default 2000, for payments made after
2025-12-31 per vendor sources; confirm with your tax advisor). `GET /compliance/ytd.csv` exports the same rows.
`GET /employers/{eid}/contractors/{cid}/history` returns the change log, payments and onboarding invites.
