# DRAFT — pending Sriram's review before publishing

# Announcement Post — PaySilo's LangGraph Agent

*Draft for the HackerEarth submission page and LinkedIn (Team STATIC).
Do not publish until Sriram has reviewed it.*

---

We built an AI agent that moves real payroll money — and then we spent
most of our time making sure it cannot move the *wrong* money.

**PaySilo** is a multi-employer payroll executor built by Team STATIC
for the Airwallex Agentic Banking Hackathon. A payroll agency runs
payroll for many client companies from one platform. Each employer's
funds live in their own silo — their own Airwallex connected account,
wallet and ledger — and the agent can never move money between silos.

The agent is a LangGraph supervisor running one independent subgraph
per employer. Only one node uses a language model: the Intake Agent
(Claude), which reads messy payroll emails and sheets into structured
pay lines. Everything after that is deterministic code — pricing
(SWIFT fees priced before FX), affordability (payroll + run fee + a
USD 1,000 reserve floor, checked against the employer's own wallet
only), routing, and idempotent execution. Our rule: **the model reads,
the code decides, the API moves the money.**

And the API moves money only on a human decision. Every run pauses at
a LangGraph interrupt; resuming requires a Visa TAP-signed approval
over the exact run, amounts and beneficiaries. The chat assistant,
Silo, can start a run and explain it — it has no approve tool and no
transfer tool, by design. When a run settles, the ledger reconciles
and a hash of the receipt is anchored via Metal. Paused runs survive
restarts through a durable checkpointer, and the same graph is wrapped
for deployment on AWS Bedrock AgentCore.

The demo tells the story in numbers: Acme pays EUR 6,025.70 and
settles at USD 13,343.24. Borealis is short by USD 2,642.88 and is
held — even when a USD 10,000 deposit lands in another employer's
wallet mid-run. Safety is not a claim in a prompt; it is a test suite:
end-to-end money tests, scenario tests, and an adversarial suite
(prompt injection in payroll emails, cross-tenant requests, replayed
approvals) where every attack ends with no money moved.

One honest note: our live Airwallex sandbox access is still pending,
so the current demo runs on an in-repo simulator that implements the
same interface as the sandbox client — the graph, policy and signing
code are identical either way, and Metal receipts are labelled `mock`
until the live chain is configured. We would rather label that clearly
than hide it.

Partner tools, each with one concrete job: **Airwallex** (connected
accounts, FX and payouts), **Visa TAP** (signed approvals and payout
instructions), **Metal** (receipt anchoring), **Claude** (intake
reading), and **AWS Bedrock AgentCore** (agent deployment runtime).

— Team STATIC
