# Deploying PaySilo on AWS Bedrock AgentCore

PaySilo's payroll graph is deployment-agnostic: the **same LangGraph graph**
runs in the FastAPI app locally and as an AgentCore Runtime agent. Only the
entrypoint wrapper differs.

## Shape

```
Operator dashboard (Next.js)
        |
   FastAPI (local)            AgentCore Runtime (AWS)
        \                    /
         \                  /
          app/agents/graph.py   <- one graph, deterministic code supervisor
                  |
     Airwallex REST (x-on-behalf-of) / simulator in demo mode
```

- **Entrypoint** — `backend/app/partners/agentcore.py` exposes
  `agentcore_handler(payload)`. When the `bedrock_agentcore` SDK is present
  it registers as the runtime entrypoint (`BedrockAgentCoreApp`); locally it
  is a plain function, so tests and the CLI use the identical code path.
- **Runtime config** — see `agentcore.json` in this folder: container built
  from `backend/Dockerfile`, HTTP protocol, entrypoint above.
- **Gateway** — AgentCore Gateway can expose the Airwallex tool surface for
  discovery/observability. The money path stays REST with `x-on-behalf-of`:
  MCP-style gateways cannot act on behalf of a connected account, and
  employer-scoped calls must carry that header (Starter Kit 6 rule).
- **Observability** — the graph appends a human-readable audit line for
  every decision (pricing, shortfall, guard refusal, fee, reconciliation).
  On AgentCore these map to CloudWatch traces via AgentCore Observability,
  giving judges the same replay the local demo prints.
- **Checkpoints** — locally `MemorySaver`. On AgentCore set `DATABASE_URL`
  and swap in the LangGraph Postgres checkpointer so a run paused at the
  approval/escalation interrupt survives a container restart. The graph
  definition does not change.

## Deploy sketch

1. Build & push the backend container to ECR (`backend/Dockerfile`).
2. Create the AgentCore Runtime pointing at the image + the entrypoint in
   `agentcore.json`; set env from `.env.example` (Airwallex sandbox keys,
   `APP_ENV=live`, `DATABASE_URL`, Metal vars when the testnet wallet is
   faucet-funded).
3. Smoke test with `{"action": "demo"}` — against the sandbox this runs
   the Kit 6 scenario through the real client; the response must show
   Acme SETTLED, Borealis HELD_SHORTFALL, platform fees USD 149.
