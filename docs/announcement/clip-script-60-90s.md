# DRAFT — pending Sriram's review before publishing or recording for release

# PaySilo — 60–90 Second Announcement Clip Script

**Team STATIC · Airwallex Agentic Banking Hackathon**

Reuse note: Shots 2–4 are lifted from the full demo script
(`docs/demo-script.md`, Act 1) so this clip can be cut from the same
recording session. Environment note, same as the demo script: footage
is captured on the in-repo Airwallex simulator until sandbox keys land;
the flow and numbers are identical on the sandbox. Total runtime
target: **75 seconds**.

| Time | Shot | On screen | Narration |
|---|---|---|---|
| 0:00–0:08 | 1. Cold open | PaySilo overview: three employers — Acme USD 20,000, Borealis USD 5,000, Cobalt empty. Platform total highlighted, then crossed out. | "Three companies. One payroll platform. And a platform total that should never be trusted — because none of that money belongs to the platform." |
| 0:08–0:20 | 2. Chat | Silo chat panel. Type: `Run payroll for Acme`. The run starts and **pauses**; approval card shows EUR 6,025.70 including 2 × EUR 12.85 SWIFT fees, plus the USD 149 run fee. | "One sentence in chat, and PaySilo's LangGraph agent reads the instruction, prices the SWIFT fees before conversion, and checks Acme's own wallet. Then it stops — on purpose." |
| 0:20–0:32 | 3. The refusal | In chat, type: `approve the run`. Silo declines; run still RESERVED; balance unchanged at USD 20,000. | "The agent cannot approve its own work. There is no approve button in chat, no transfer tool for the model to call. Money moves only on a human decision — signed with Visa TAP over the exact amounts." |
| 0:32–0:52 | 4. The payment | Run panel: click **Approve**. Timeline fills in: FX conversion, both payouts PAID with transfer IDs, USD 149 fee, reconciliation, Metal receipt anchored. Balance lands on **USD 13,343.24**. | "A human approves, the signature verifies, and Airwallex moves the money — from Acme's connected account, on Acme's behalf, to Acme's contractors. The run fee is collected, the ledger reconciles, and the receipt hash is anchored with Metal." |
| 0:52–1:08 | 5. The hold | Borealis run starts and **escalates**: HELD_SHORTFALL, short **USD 2,642.88**. Cobalt's USD 10,000 deposit lands beside it — Borealis stays held. | "Borealis is short by two thousand six hundred and forty-two dollars. Even when ten thousand dollars lands next door in Cobalt's wallet, Borealis stays held. Silos are not a prompt instruction — they are the ledger." |
| 1:08–1:15 | 6. Close | Overview: one settled run, platform fees exactly USD 149. Title card: PaySilo — Team STATIC. | "The model reads. The code decides. The API moves the money — only ever its owner's money." |

## Production notes

- Record at the same session as the full demo video; reuse the Act 1
  screen capture for Shots 2–4 and re-record only the narration.
- Keep the receipt's `mock` chain label visible in Shot 4 (honesty
  rule); if sandbox keys have landed, re-capture on the sandbox and
  update this note before publishing.
- Captions: burn in the three numbers — EUR 6,025.70, USD 2,642.88,
  USD 13,343.24. They are the story.
