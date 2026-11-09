"""LangGraph v2: durable checkpoints, supervisor batch, FAILED-run resume.

The resumability proof uses the file-backed SQLite checkpointer: a brand
new graph instance built on the same checkpoint file (a simulated process
restart) resumes a run paused at approval. PostgresSaver shares the same
LangGraph checkpointer interface and is wired in the factory for
production, but there is no Postgres server in this environment to run a
live restart against.
"""

from decimal import Decimal

from langgraph.types import Command

from app.agents.checkpoint import build_checkpointer
from app.agents.demo import EMPLOYERS, build_demo_services, demo_resume
from app.agents.graph import build_graph, initial_state, resume_failed
from app.agents.supervisor import run_batch

ACME_LINES = EMPLOYERS["acme"]["pay_lines"]


def _signed_resume(services, graph, config):
    state = graph.get_state(config).values
    payload = graph.get_state(config).interrupts[0].value if graph.get_state(config).interrupts else {}
    return demo_resume(services, state, payload)


def test_paused_run_survives_restart_via_file_checkpointer(tmp_path):
    checkpoint_file = str(tmp_path / "checkpoints.sqlite")
    services = build_demo_services()  # external systems (Airwallex/ledger) persist across a restart

    # Process 1: start Acme's run, it pauses at the approval interrupt.
    graph1 = build_graph(services, checkpointer=build_checkpointer(checkpoint_path=checkpoint_file))
    config = {"configurable": {"thread_id": "run-restart"}}
    result = graph1.invoke(
        initial_state(run_id="run-restart", employer_key="acme", employer_name="Acme Manufacturing GmbH",
                      funding_usd="20000", pay_lines=ACME_LINES), config)
    assert result["__interrupt__"][0].value["kind"] == "approval"
    assert services.ledger.is_balanced()

    # Simulated restart: a NEW graph + NEW checkpointer on the same file.
    graph2 = build_graph(services, checkpointer=build_checkpointer(checkpoint_path=checkpoint_file))
    paused = graph2.get_state(config).values
    assert paused["status"] == "RESERVED"  # the pause itself survived

    graph2.invoke(Command(resume=_signed_resume(services, graph2, config)), config)
    final = graph2.get_state(config).values
    assert final["status"] == "SETTLED"
    assert final["fee_charged_usd"] == "149.00"
    # 20000 - (6025.70 EUR * 1.08) - 149 fee = 13343.24 (the demo's golden number)
    assert Decimal(final["balances"]["USD"]) == Decimal("13343.24")
    assert services.ledger.is_balanced()


def test_runtime_paused_run_survives_app_restart_with_checkpoint_path(tmp_path):
    """End-to-end through PlatformRuntime: pause, 'restart' (new app, new
    runtime, same DB file + same checkpoint file), approve, settles."""
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.config import Settings
    from app.db import Database
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    url = f"sqlite:///{(tmp_path / 'paysilo.db').as_posix()}"
    settings = Settings(database_url=url, jwt_secret="ckpt-secret-" + "x" * 32,
                        checkpoint_path=str(tmp_path / "checkpoints.sqlite"))
    login = {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}

    with TestClient(create_app(settings, Database.from_url(url))) as c:
        c.post("/auth/login", json=login)
        acme = next(e for e in c.get("/employers").json() if e["name"].startswith("Acme"))
        detail = c.get(f"/employers/{acme['id']}").json()
        run = c.post("/runs", json={"employer_id": acme["id"],
                                    "lines": [{"contractor_id": x["id"]} for x in detail["contractors"]]}).json()
        assert run["status"] == "RESERVED" and run["resumable"]

    with TestClient(create_app(settings, Database.from_url(url))) as c:  # the "restart"
        c.post("/auth/login", json=login)
        paused = c.get(f"/runs/{run['id']}").json()
        assert paused["status"] == "RESERVED" and paused["resumable"] is True
        done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
        assert done["status"] == "SETTLED", done
        assert done["fee_charged_usd"] == "149.00"
        acme = next(e for e in c.get("/employers").json() if e["name"].startswith("Acme"))
        assert acme["balances"] == {"USD": "13343.24"}


def test_batch_supervisor_settles_holds_and_leaves_cobalt_untouched():
    services = build_demo_services()
    specs = [
        {"key": key, "name": spec["name"], "contact_name": spec["contact"],
         "funding_usd": spec["funding_usd"], "pay_lines": spec["pay_lines"]}
        for key, spec in EMPLOYERS.items()
    ]
    results = run_batch(services, specs, decider=demo_resume)

    assert results["acme"]["outcome"] == "settled"
    assert results["acme"]["status"] == "SETTLED"
    assert Decimal(results["acme"]["balances"]["USD"]) == Decimal("13343.24")

    assert results["borealis"]["outcome"] == "held"
    assert results["borealis"]["status"] == "HELD_SHORTFALL"
    assert results["borealis"]["shortfall_usd"] == "2642.88"
    assert results["borealis"]["fee_charged_usd"] == "0.00"

    assert results["cobalt"]["outcome"] == "untouched"
    assert results["cobalt"]["status"] == "NO_PAYROLL_DUE"

    # Isolation: only Acme's fee reached the platform; ledgers balance.
    assert services.airwallex.balance_of("USD") == Decimal("149.00")
    assert services.ledger.is_balanced()


def test_batch_one_employer_failure_does_not_affect_others():
    services = build_demo_services()
    bad_line = {"name": "Nobody", "amount": "100", "currency": "EUR", "method": "SWIFT"}  # no bank details
    specs = [
        {"key": "broken", "name": "Broken Co", "funding_usd": "1000", "pay_lines": [bad_line]},
        {"key": "acme", "name": "Acme Manufacturing GmbH", "contact_name": "Ana Weber",
         "funding_usd": "20000", "pay_lines": ACME_LINES},
    ]
    results = run_batch(services, specs, decider=demo_resume)
    assert results["broken"]["outcome"] == "failed"  # policy refusal, contained
    assert results["acme"]["outcome"] == "settled"


class _FlakyAirwallex:
    """Wraps the simulator; the first create_transfer raises AFTER the inner
    call succeeded (a lost response), like a transient network failure."""

    def __init__(self, inner):
        self.inner = inner
        self.failures_left = 1

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def create_transfer(self, **kwargs):
        result = self.inner.create_transfer(**kwargs)
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("simulated transient Airwallex failure (response lost)")
        return result


def test_failed_run_resumes_from_checkpoint_without_duplicate_payment():
    from app.agents.graph import Services
    from app.airwallex.simulator import USD_VALUE, AirwallexSimulator
    from app.ledger import Ledger
    from app.partners.metal import MockSettlementChain

    base = build_demo_services()
    services = Services(airwallex=_FlakyAirwallex(base.airwallex), ledger=Ledger(),
                        chain=MockSettlementChain(), usd_rates=dict(USD_VALUE), tap=base.tap)
    graph = build_graph(services)
    config = {"configurable": {"thread_id": "run-flaky"}}
    result = graph.invoke(
        initial_state(run_id="run-flaky", employer_key="acme", employer_name="Acme Manufacturing GmbH",
                      funding_usd="20000", pay_lines=ACME_LINES), config)
    assert result["__interrupt__"][0].value["kind"] == "approval"
    graph.invoke(Command(resume=_signed_resume(services, graph, config)), config)
    failed = graph.get_state(config).values
    assert failed["status"] == "FAILED"

    resume_failed(graph, config)
    final = graph.get_state(config).values
    assert final["status"] == "SETTLED"
    # Paid exactly once despite the replay: same golden balance as a clean run.
    assert Decimal(final["balances"]["USD"]) == Decimal("13343.24")
    assert services.ledger.is_balanced()
    # Exactly two transfers exist in the simulator (one per contractor).
    assert len(services.airwallex.inner._transfers) == 2
