# PaySilo — Demo Video Script (under 5 minutes)

**Team STATIC · Airwallex Agentic Banking Hackathon · Starter Kit 6**

> **Environment note — read first.** This script runs on the in-repo
> **Airwallex simulator** (demo mode, no API keys). The live Airwallex
> sandbox is blocked until sandbox keys and connected-account access land
> for the team. Every screen, number and behaviour below is identical on
> the simulator; when the keys arrive, the same flow runs against the
> sandbox REST client with no script changes, and transfer IDs become
> live sandbox IDs. Metal receipts in this recording are on the mock
> chain and are labelled `mock` on screen — that label is part of the
> product's honesty rules, keep it visible.

**Demo login:** `demo@paysilo.test` / `SiloDemo2026`
**Organisation on screen:** Vertex Payroll Services (a payroll agency
running payroll for three client employers).

Cast of employers:

| Employer | Wallet at start | Owes | Outcome |
|---|---|---|---|
| Acme Manufacturing GmbH | USD 20,000 | Lena Fischer EUR 3,400 + Tomas Rivera EUR 2,600 | **Pays — SETTLED** |
| Borealis Labs AB | USD 5,000 | Ingrid Solberg EUR 6,000 | **Held — short USD 2,642.88** |
| Cobalt Retail LLC | USD 0 | Nothing due | **Untouched; its deposit helps nobody else** |

Policy numbers used throughout: SWIFT payout fee **EUR 12.85 per payout**
(priced before FX), platform run fee **USD 149** (only when a payroll
actually runs), reserve floor **USD 1,000** that must stay in the wallet.
FX rate on screen: 1 EUR = 1.08 USD.

---

## Shot list and narration

Total target runtime: **4 minutes 30 seconds**.

### 0:00–0:25 — The problem (dashboard, all three employers visible)

- **Do:** Log in with the demo account. Land on the overview showing the
  three employers and their balances: Acme USD 20,000.00, Borealis
  USD 5,000.00, Cobalt empty.
- **Say:** "Vertex Payroll Services runs payroll for other companies.
  The platform total looks healthy — but that total is an illusion. Each
  employer's money lives in its own silo: its own Airwallex connected
  account, its own wallet, its own ledger. PaySilo's agent can never
  move money between silos. Watch what that means."

### 0:25–1:50 — Act 1: Acme pays, from chat (Silo chat panel)

- **Type in chat, exactly:** `Run payroll for Acme`
- **Expected on screen:** Silo starts the run and immediately **pauses**.
  An approval card appears: Acme payroll of EUR 6,025.70 (salaries
  EUR 6,000 + 2 × EUR 12.85 SWIFT fees), about USD 6,507.76, plus the
  USD 149 run fee. Run status: **RESERVED**. Acme's balance is still
  USD 20,000.00.
- **Say:** "One sentence in chat, and the agent did everything except
  the one thing it must never do. Intake read the instruction, pricing
  priced the SWIFT fees before any conversion, and the policy engine
  checked Acme's own wallet against payroll, run fee and the thousand
  dollar reserve. Then it stopped. Money moves only on a human decision,
  signed with a TAP-style signature over these exact amounts and beneficiaries."
- **Type in chat, exactly:** `approve the run`
- **Expected on screen:** Silo explains it cannot approve; the run is
  still RESERVED and the balance is unchanged.
- **Say:** "Not even if you ask nicely in chat. There is no approve tool
  and no transfer tool in the chat surface at all."
- **Do:** Open the run and click **Approve** on the run itself (the
  signed human decision).
- **Expected on screen:** The run executes step by step in the timeline:
  FX conversion USD → EUR, both payouts marked PAID with Airwallex
  transfer IDs, the USD 149 platform fee, reconciliation, and a Metal
  receipt anchored (chain: mock). Run status: **SETTLED**. Acme's
  balance: **USD 13,343.24**. Platform fees: **USD 149.00**.
- **Say:** "Approved by a human, signed, executed on Acme's behalf only.
  Twenty thousand minus six thousand five hundred and seven dollars of
  converted payroll, minus the one hundred and forty-nine dollar fee,
  leaves thirteen thousand three hundred and forty-three dollars and
  twenty-four cents. Every step is in the audit timeline, and the
  reconciliation receipt is hash-anchored."

### 1:50–3:05 — Act 2: Borealis is held (employers → Borealis Labs AB)

- **Do:** Start a payroll run for Borealis with its one contractor,
  Ingrid Solberg (EUR 6,000 default).
- **Expected on screen:** The run does **not** pause for approval — it
  escalates. Status: **HELD_SHORTFALL**, shortfall **USD 2,642.88**,
  fee charged USD 0.00.
- **Type in chat, exactly:** `Why is Borealis held?`
- **Expected on screen:** Silo answers with the stored assessment math:
  EUR 6,012.85 needed (salary + one SWIFT fee) is about USD 6,493.88,
  plus the USD 149 run fee is USD 6,642.88 leaving the wallet, and the
  USD 1,000 reserve must stay — USD 7,642.88 needed against a USD 5,000
  wallet.
- **Say:** "Borealis owes six thousand euros but only loaded five
  thousand dollars. Salary plus fee is six thousand four hundred and
  ninety-three dollars; add the run fee and the reserve it must keep,
  and it is short by exactly two thousand six hundred and forty-two
  dollars and eighty-eight cents. A tired accountant would cover it
  from the platform total. The agent cannot: it only ever looks at
  Borealis's own wallet, so the payroll is held, escalated to a human,
  and charged no fee."
- **Do:** On the held run, try the decision **Approve**.
- **Expected on screen:** Refused (409): a short payroll can only be
  held. Then choose **Hold**.
- **Say:** "Even a human cannot approve money that is not there. The
  only ways forward are Borealis's own: top up its own wallet, or
  reduce the payroll."

### 3:05–4:00 — Act 3: Cobalt's deposit changes nothing for Borealis

- **Do:** Open Cobalt Retail LLC and fund it with **USD 10,000**
  (demo deposit).
- **Expected on screen:** Cobalt's balance becomes USD 10,000.00.
  Borealis's balance is still USD 5,000.00.
- **Do:** Start a fresh Borealis run.
- **Expected on screen:** Still **HELD_SHORTFALL**, still short
  **USD 2,642.88**.
- **Say:** "Mid-story, ten thousand dollars lands — in Cobalt's silo.
  It fixes Cobalt's balance and nobody else's. Borealis is re-checked
  against its own wallet and is still short by the same two thousand
  six hundred and forty-two dollars and eighty-eight cents. That is
  tenant isolation enforced by the ledger and the policy engine, not
  by a prompt."

### 4:00–4:30 — Close (overview)

- **Expected on screen:** Overview: one run settled, platform fees
  exactly **USD 149.00**, Cobalt untouched by any payroll, Borealis
  held with its shortfall explained.
- **Say:** "The model reads, the code decides, the API moves the money —
  and only ever its owner's money. That is PaySilo."

---

## Pre-recording checklist

- [ ] Fresh demo seed (restart the backend) so balances are exactly
      USD 20,000 / USD 5,000 / empty and no runs exist.
- [ ] Confirm the overview shows platform fees as empty before Act 1.
- [ ] Keep the `mock` chain label visible on the receipt; do not crop it.
- [ ] If sandbox keys have landed by recording day, re-run this script
      on the sandbox first and update the environment note above; do not
      mix simulator and sandbox footage in one take.
