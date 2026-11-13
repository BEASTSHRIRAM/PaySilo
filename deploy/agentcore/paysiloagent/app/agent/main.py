"""PaySilo payroll agent on Amazon Bedrock AgentCore Runtime.

AgentCore Runtime calls the function marked @runtime.entrypoint with the JSON payload of each
invocation. PaySilo's LangGraph payroll graph (app/agents/graph.py) runs unchanged inside it:
the same intake -> risk -> pricing -> affordability -> approval -> payout -> reconcile graph that
the FastAPI backend runs. This entrypoint runs it against the built-in simulator, so it needs no
Airwallex keys and moves no money.

Payload: {"action": "demo"}   (default)
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from app.partners.agentcore import agentcore_handler

runtime = BedrockAgentCoreApp()


@runtime.entrypoint
def invoke(payload, context=None):
    return agentcore_handler(payload or {})


if __name__ == "__main__":
    runtime.run()
