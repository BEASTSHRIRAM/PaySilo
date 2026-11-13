"""Calls the PaySilo agent deployed on Amazon Bedrock AgentCore Runtime to re compute a run's pricing plan.

The backend stays the decision maker: it prices the run itself, then asks the agent on AgentCore to
price the same facts (``app/agents/cloud_plan.py``). The pricing node compares the two results.

Credentials come from the standard AWS chain (an EC2 instance role in production, ``aws configure``
locally). No keys live in PaySilo's settings.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

logger = logging.getLogger("paysilo.bedrock")


class BedrockPlannerError(Exception):
    pass


class BedrockPlanner:
    def __init__(self, runtime_arn: str, region: str | None = None, timeout_s: float = 20.0) -> None:
        import boto3
        from botocore.config import Config

        self.runtime_arn = runtime_arn
        # arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
        self.region = region or runtime_arn.split(":")[3]
        self.runtime_id = runtime_arn.rsplit("/", 1)[-1]
        self._client = boto3.client(
            "bedrock-agentcore", region_name=self.region,
            config=Config(connect_timeout=5, read_timeout=timeout_s, retries={"max_attempts": 2}))

    def plan(self, payload: dict) -> dict:
        """Returns the agent's plan plus where it ran. Raises on transport or agent errors."""
        session_id = f"paysilo-plan-{uuid.uuid4().hex}"  # AgentCore needs 33+ characters
        started = time.monotonic()
        resp = self._client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn, runtimeSessionId=session_id,
            contentType="application/json", accept="application/json",
            payload=json.dumps({"action": "plan", "plan": payload}).encode("utf-8"))
        body = resp["response"].read()
        ms = int((time.monotonic() - started) * 1000)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise BedrockPlannerError("AgentCore returned a response that is not JSON.") from exc
        if not isinstance(data, dict) or data.get("action") != "plan" or "error" in data:
            raise BedrockPlannerError("AgentCore did not return a plan.")
        logger.info("Bedrock AgentCore plan: runtime=%s session=%s ms=%s", self.runtime_id, session_id, ms)
        return {**data, "region": self.region, "runtime_id": self.runtime_id, "session_id": session_id, "ms": ms}
