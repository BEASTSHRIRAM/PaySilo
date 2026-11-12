# Proof of payment (PDF)

An agency pays contractors on behalf of employers. When a contractor says "I never received the money", the
employer asks the agency, and the agency needs something it can hand over. PaySilo generates it from facts it
actually stores.

## Endpoints

| Endpoint | Who | What |
| --- | --- | --- |
| `GET /runs/{run}/lines/{line}/proof.pdf` | any role in the organization (viewers too) | Proof of payment for a PAID line, **Payment status notice** for anything else |
| `GET /runs/{run}/statement.pdf` | same | Run-level payment statement listing every line |
| `GET /runs/{run}/lines/{line}/trace` | same | "Where is my money?" diagnosis (JSON) |
| `GET /receipts/{line}/proof.pdf?token=` | contractor (existing HMAC receipt-link token) | The same PDF; wrong/expired token is a 404 |
| `GET /verify/{token}` | public, no login | Re-computes the integrity check, returns VALID / INVALID with key facts |

Foreign ids are 404. Every generation writes a row to `proof_accesses` (who, which run/line, kind, channel,
SHA-256 of the canonical record). The Next.js page `/verify/[token]` is the human face of `GET /verify/{token}`
(the QR code in each PDF points to it; set `PUBLIC_APP_URL` so links use your public host).

## What the PDF contains

Agency and employer names, run id and dates, contractor name, payout amount and currency, payout method and fee,
FX conversion and rate when one was recorded, the destination account **masked to the last 4 characters** (plus
SWIFT/BIC and country when stored; the bank name is not stored), the Airwallex transfer id and request id, a status
timeline with the timestamps PaySilo recorded (run created, approval with TAP key id and verified flag, Airwallex
transfer created, Airwallex reports PAID, run settled), the ledger receipt hash, an integrity block (HMAC-SHA256 and
a verification URL/QR), and a plain-language "What to tell the contractor" section.

Lines that are failed, held, returned or not yet sent produce a **Payment status notice** that says NOT PAID and
why (the friendly run-error text and the Airwallex failure code meaning). A notice is never worded as a proof.

## Integrity

The canonical JSON of the facts is signed with HMAC-SHA256 using a key derived from the session secret under its
own label (`paysilo/proof-of-payment/v1`), the same construction as the receipt-link key. The token is
`l.<pay-line-id>.<hmac>` (or `r.<run-id>.<hmac>` for a statement). `/verify` recomputes the facts from the database
and compares in constant time. So:

- a PDF whose text was edited (or whose QR/token was swapped) fails: the code does not match the record;
- if the database record behind a document changes afterwards (amount, status, transfer id, masked account...), the
  old document turns INVALID. That is deliberate. A held line that is later paid will therefore need a new document.
- the public page shows names, amounts and the masked account only, never approver emails, never full account numbers.

## What it does NOT prove

- **That the contractor's bank credited the account.** PaySilo only knows what Airwallex reported. The receiving
  bank's own credit time is not available to PaySilo. Typical settlement: SEPA / ACH / FPS 0 to 2 business days,
  SWIFT 1 to 5 (a rule of thumb, not a bank promise).
- That the contractor owns the destination account; only that the payout instruction went to the account ending in
  the masked digits.
- Anything on a server that is not PaySilo: a database administrator with the signing secret could re-sign altered
  data. The Metal anchor of the run receipt is the external commitment.
- In demo mode the Airwallex data comes from the in-repo simulator, not a live provider.

## "Where is my money?" states

`payment_trace(line, run)` is deterministic code (no LLM):

| State | Meaning |
| --- | --- |
| SCHEDULED | no transfer exists; the run awaits approval or execution |
| SENT | a transfer exists but Airwallex has not reported PAID |
| PAID | Airwallex reports PAID; check with the bank using the transfer id |
| HELD | the line was held (shortfall, human hold, rejected, refused) |
| FAILED | the payout failed; the Airwallex code, when known, is explained |
| RETURNED | the receiving bank sent it back (code 90802 or a RETURNED transfer status) |

Airwallex failure codes handled: **90301** beneficiary name mismatch, **90302** currency mismatch,
**90401** invalid beneficiary details, **90802** beneficiary bank returned the payment. The Silo chat has a read-only
`where_is_my_money` tool backed by the same function.
