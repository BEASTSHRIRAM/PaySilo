# PaySilo on Amazon Bedrock AgentCore Runtime

PaySilo's LangGraph payroll graph (`backend/app/agents/graph.py`) runs unchanged on AgentCore
Runtime. Only the entrypoint differs.

## How the wrap works

`paysiloagent/app/agent/main.py`:

```python
runtime = BedrockAgentCoreApp()

@runtime.entrypoint
def invoke(payload, context=None):
    return agentcore_handler(payload or {})
```

1. `BedrockAgentCoreApp()` turns the file into the HTTP service AgentCore Runtime expects.
2. `@runtime.entrypoint` marks the function AWS calls for each invocation, with the request JSON as `payload`.
3. `agentcore_handler` (`backend/app/partners/agentcore.py`) builds and runs the graph.

The deployed action is `{"action": "demo"}`: the Starter Kit 6 scenario on the built in simulator.
It needs no Airwallex keys and moves no money. Expected result: Acme SETTLED, Borealis
HELD_SHORTFALL with a USD 2,642.88 shortfall, platform fees USD 149.00.

## Deploy

```bash
npm install -g @aws/agentcore        # CLI 1.x
aws configure                        # credentials for the target account
cd deploy/agentcore/paysiloagent
./sync-and-deploy.sh ap-southeast-2
```

The script copies `backend/app` into the agent folder (CodeZip build, Python 3.12), deploys through
CDK and invokes the agent once. `agentcore/aws-targets.json` is created on first deploy.

## What stays on the backend

Runs paused for human approval, the signed approval itself and live payouts stay on the FastAPI
backend (single instance). AgentCore sessions are short lived, so they are the wrong place to hold
a run that waits for a person.
