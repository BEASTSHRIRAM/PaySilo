"""AWS Bedrock AgentCore entrypoint wrapper.

The same payroll graph that runs locally runs on AgentCore Runtime: this
module exposes a plain handler that invokes the graph/demo and returns
its result as JSON-safe data. When the ``bedrock_agentcore`` SDK is
installed (in the AgentCore container), the handler is registered as the
runtime entrypoint; locally the SDK import is optional and the plain
handler is used directly (tests, CLI, FastAPI all share it).

See ``deploy/agentcore/README.md`` for the deployment shape: container ->
AgentCore Runtime, Gateway exposing the Airwallex tool surface, and
AgentCore Observability feeding the audit trail.
"""

from __future__ import annotations

from typing import Any

from ..agents.demo import run_demo


def agentcore_handler(payload: dict | None = None) -> dict[str, Any]:
    """Plain handler: invoke the payroll demo graph and return its report.

    Payload: ``{"action": "demo"}`` (default). The demo runs against the
    in-memory simulator unless the deployment injects live services —
    the graph code is identical either way.
    """
    payload = payload or {}
    action = payload.get("action", "demo")
    if action == "demo":
        return {"action": "demo", "report": run_demo()}
    if action == "plan":
        # One real payroll run's pricing plan, re computed here from the facts the backend sent.
        from ..agents.cloud_plan import run_plan

        return {"action": "plan", "runtime": "bedrock-agentcore", **run_plan(payload.get("plan") or {})}
    return {"action": action, "error": f"Unknown action {action!r}; supported: 'demo', 'plan'"}


try:  # Register as the AgentCore Runtime entrypoint when the SDK exists.
    from bedrock_agentcore import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def _entrypoint(payload: dict | None = None) -> dict[str, Any]:
        return agentcore_handler(payload)

except ImportError:  # Local/dev: no SDK, plain handler only.
    app = None
