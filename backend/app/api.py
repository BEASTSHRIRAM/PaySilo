"""FastAPI surface for PaySilo.

Demo mode (``APP_ENV=demo`` or no Airwallex keys): the in-memory
simulator backs the graph, so everything below works with zero keys.
Live mode swaps in the real Airwallex REST client — same graph, same API.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command
from pydantic import BaseModel

from .agents.demo import EMPLOYERS, run_demo
from .agents.graph import Services, build_graph, initial_state
from .airwallex.client import AirwallexClient
from .airwallex.simulator import USD_VALUE, AirwallexSimulator
from .config import Settings, load_settings
from .ledger import Ledger
from .partners.metal import MockSettlementChain


class Runtime:
    """Holds the shared services, the compiled graph, and run snapshots."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.services = self._make_services()
        self.graph = build_graph(self.services)
        self.runs: dict[str, dict] = {}
        self.demo_report: dict | None = None

    def _make_services(self) -> Services:
        airwallex = (
            AirwallexSimulator() if self.settings.demo_mode else AirwallexClient(self.settings)
        )
        return Services(
            airwallex=airwallex, ledger=Ledger(),
            chain=MockSettlementChain(), usd_rates=dict(USD_VALUE),
        )

    def reset(self) -> None:
        self.services = self._make_services()
        self.graph = build_graph(self.services)
        self.runs = {}

    # -- run driving ---------------------------------------------------------
    def _snapshot(self, run_id: str, result: dict | None) -> dict:
        config = {"configurable": {"thread_id": run_id}}
        values = dict(self.graph.get_state(config).values)
        pending = None
        if result is not None and "__interrupt__" in result:
            pending = result["__interrupt__"][0].value
        snapshot = {
            "run_id": run_id,
            "employer": values.get("employer_name"),
            "employer_key": values.get("employer_key"),
            "status": values.get("status"),
            "assessment": values.get("assessment"),
            "fee_charged_usd": values.get("fee_charged_usd"),
            "explanation": values.get("explanation"),
            "balances": values.get("balances"),
            "metal_receipt": values.get("metal_receipt"),
            "audit": values.get("audit", []),
            "pending_interrupt": pending,
            "error": values.get("error"),
        }
        self.runs[run_id] = snapshot
        return snapshot

    def start_run(self, spec: dict) -> dict:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        state = initial_state(
            run_id=run_id, employer_key=spec["employer_key"],
            employer_name=spec["employer_name"], contact_name=spec.get("contact", ""),
            funding_usd=spec.get("funding_usd", "0"), pay_lines=spec.get("pay_lines", []),
        )
        config = {"configurable": {"thread_id": run_id}}
        result = self.graph.invoke(state, config)
        return self._snapshot(run_id, result)

    def resume_run(self, run_id: str, decision: str) -> dict:
        if run_id not in self.runs:
            raise HTTPException(404, f"Unknown run {run_id}")
        if self.runs[run_id]["pending_interrupt"] is None:
            raise HTTPException(409, f"Run {run_id} is not waiting for a decision")
        config = {"configurable": {"thread_id": run_id}}
        result = self.graph.invoke(Command(resume=decision), config)
        return self._snapshot(run_id, result)


class RunRequest(BaseModel):
    employer_key: str = "acme"
    employer_name: str | None = None
    contact: str = ""
    funding_usd: str | None = None
    pay_lines: list[dict] | None = None


class DecisionRequest(BaseModel):
    decision: str  # "approve" | "hold"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    runtime = Runtime(settings)
    app = FastAPI(title="PaySilo", version="0.1.0",
                  description="Multi-Employer Payroll Executor — the model reads, the code decides, the API moves the money.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.runtime = runtime

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": "demo" if settings.demo_mode else "live"}

    @app.get("/employers")
    def employers() -> list[dict]:
        out = []
        report = runtime.demo_report or {}
        for key, spec in EMPLOYERS.items():
            entry = {"key": key, "name": spec["name"], "contact": spec["contact"],
                     "funding_usd": spec["funding_usd"], "balances": {}, "status": "NOT_RUN"}
            if key in report.get("employers", {}):
                entry["balances"] = report["employers"][key]["balances"]
                entry["status"] = report["employers"][key]["status"]
            elif key == "cobalt" and "cobalt" in report:
                entry["balances"] = report["cobalt"]["balances"]
                entry["status"] = report["cobalt"]["payroll_status"]
            out.append(entry)
        return out

    @app.post("/runs/demo")
    def demo() -> dict:
        runtime.reset()
        runtime.demo_report = run_demo(runtime.services)
        return runtime.demo_report

    @app.post("/runs")
    def create_run(req: RunRequest) -> dict:
        spec = dict(EMPLOYERS.get(req.employer_key, {}))
        spec["employer_key"] = req.employer_key
        spec["employer_name"] = req.employer_name or spec.get("name") or req.employer_key
        if req.funding_usd is not None:
            spec["funding_usd"] = req.funding_usd
        if req.pay_lines is not None:
            spec["pay_lines"] = req.pay_lines
        spec.setdefault("funding_usd", "0")
        spec.setdefault("pay_lines", [])
        spec["contact"] = req.contact or spec.get("contact", "")
        return runtime.start_run(spec)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        if run_id not in runtime.runs:
            raise HTTPException(404, f"Unknown run {run_id}")
        return runtime.runs[run_id]

    @app.post("/runs/{run_id}/approve")
    def approve(run_id: str, req: DecisionRequest) -> dict:
        return runtime.resume_run(run_id, req.decision)

    @app.get("/audit")
    def audit() -> dict:
        lines: list[str] = []
        if runtime.demo_report:
            lines.extend(runtime.demo_report["audit"])
        for snap in runtime.runs.values():
            lines.extend(snap.get("audit", []))
        return {"events": lines}

    return app
