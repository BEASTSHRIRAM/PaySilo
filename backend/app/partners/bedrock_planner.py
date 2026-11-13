"""Calls the PaySilo agent deployed on Amazon Bedrock AgentCore Runtime to re compute a run's pricing plan.

The backend stays the decision maker: it prices the run itself, then asks the agent on AgentCore to
price the same facts (``app/agents/cloud_plan.py``). The pricing node compares the two results.

Credentials come from the standard AWS chain (an EC2 instance role in production, ``aws configure``
locally). No keys live in PaySilo's settings.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

logger = logging.getLogger("paysilo.bedrock")


class BedrockPlannerError(Exception):
    pass


# A session that has served a call answers in about 0.1 s; a brand new one takes about 1.5 s to start.
# The plan is stateless, so one warm session is shared by all runs and replaced before it gets old.
SESSION_MAX_AGE_S = 30 * 60
WARM_PAYLOAD = {"version": 1, "lines": [], "usd_value": {"USD": "1"}, "balances": {}, "waive_run_fee": False}


class BedrockPlanner:
    def __init__(self, runtime_arn: str, region: str | None = None, timeout_s: float = 20.0,
                 keep_warm_s: float = 240.0) -> None:
        import boto3
        from botocore.config import Config

        self.runtime_arn = runtime_arn
        # arn:aws:bedrock-agentcore:<region>:<account>:runtime/<id>
        self.region = region or runtime_arn.split(":")[3]
        self.runtime_id = runtime_arn.rsplit("/", 1)[-1]
        self._client = boto3.client(
            "bedrock-agentcore", region_name=self.region,
            config=Config(connect_timeout=5, read_timeout=timeout_s, retries={"max_attempts": 2}))
        self._lock = threading.Lock()
        self._session_id = ""
        self._session_born = 0.0
        if keep_warm_s > 0:
            threading.Thread(target=self._keep_warm, args=(keep_warm_s,), daemon=True,
                             name="bedrock-keep-warm").start()

    def _session(self, fresh: bool = False) -> str:
        with self._lock:
            if fresh or not self._session_id or time.monotonic() - self._session_born > SESSION_MAX_AGE_S:
                self._session_id = f"paysilo-plan-{uuid.uuid4().hex}"  # AgentCore needs 33+ characters
                self._session_born = time.monotonic()
            return self._session_id

    def _keep_warm(self, every_s: float) -> None:
        """Start the session early and keep it from idling out, so a payroll run never pays the cold start."""
        while True:
            try:
                self._invoke(WARM_PAYLOAD, self._session())
            except Exception as exc:  # noqa: BLE001 - a failed warm up only means the next run is slower
                logger.info("Bedrock AgentCore keep warm skipped (%s).", type(exc).__name__)
            time.sleep(every_s)

    def _invoke(self, payload: dict, session_id: str) -> dict:
        resp = self._client.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn, runtimeSessionId=session_id,
            contentType="application/json", accept="application/json",
            payload=json.dumps({"action": "plan", "plan": payload}).encode("utf-8"))
        body = resp["response"].read()
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise BedrockPlannerError("AgentCore returned a response that is not JSON.") from exc
        if not isinstance(data, dict) or data.get("action") != "plan" or "error" in data:
            raise BedrockPlannerError("AgentCore did not return a plan.")
        return data

    def plan(self, payload: dict) -> dict:
        """Returns the agent's plan plus where it ran. Raises on transport or agent errors."""
        started = time.monotonic()
        session_id = self._session()
        try:
            data = self._invoke(payload, session_id)
        except Exception:  # noqa: BLE001 - the shared session may have expired: try once on a new one
            session_id = self._session(fresh=True)
            data = self._invoke(payload, session_id)
        ms = int((time.monotonic() - started) * 1000)
        logger.info("Bedrock AgentCore plan: runtime=%s session=%s ms=%s", self.runtime_id, session_id, ms)
        return {**data, "region": self.region, "runtime_id": self.runtime_id, "session_id": session_id, "ms": ms}
