"""Issue #15 — Scenario tests for the LangGraph agent.

Scenarios: multi-currency payroll, partial contractor lists, mid-run
deposits, payout failure + retry (idempotent, no double-pay), and a
restart while a run is paused (file-backed checkpointer).

All money assertions use the demo seed's golden arithmetic
(FX 1 EUR = 1.08 USD, SWIFT fee EUR 12.85/payout, run fee USD 149,
reserve floor USD 1,000) on the Airwallex simulator — no keys exist on
this machine and none are needed.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from langgraph.types import Command

ACME = "Acme Manufacturing GmbH"
BOREALIS = "Borealis Labs AB"
COBALT = "Cobalt Retail LLC"


def _employers(c: TestClient) -> dict[str, dict]:
    return {e["name"]: e for e in c.get("/employers").json()}


def _detail(c: TestClient, name: str) -> dict:
    return c.get(f"/employers/{_employers(c)[name]['id']}").json()


def _balances(c: TestClient, name: str) -> dict:
    return _employers(c)[name]["balances"]


def _run_and_approve(c: TestClient, employer_id: str, lines: list[dict]) -> dict:
    run = c.post("/runs", json={"employer_id": employer_id, "lines": lines}).json()
    assert run["status"] == "RESERVED", run
    done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED", done
    return done


# ---------------------------------------------------------------------------
# Multi-currency payroll run (EUR SWIFT + USD LOCAL + GBP LOCAL in one run)
# ---------------------------------------------------------------------------

def test_multi_currency_run_settles_each_leg_in_its_own_currency(demo_client):
    emp = demo_client.post("/employers", json={"name": "Delta Foods BV", "country": "nl"}).json()
    demo_client.post(f"/employers/{emp['id']}/fund", json={"amount": "20000"})
    contractors = {}
    for body in (
        {"full_name": "Joris de Vries", "country": "NL", "currency": "EUR", "payout_method": "SWIFT",
         "iban": "NL91ABNA0417164300", "swift_code": "ABNANL2A", "default_amount": "1000"},
        {"full_name": "Sam Lee", "country": "US", "currency": "USD", "payout_method": "LOCAL",
         "account_number": "000123456789", "routing_code": "021000021", "default_amount": "500"},
        {"full_name": "Priya Shah", "country": "GB", "currency": "GBP", "payout_method": "LOCAL",
         "account_number": "12345678", "routing_code": "040004", "default_amount": "400"},
    ):
        r = demo_client.post(f"/employers/{emp['id']}/contractors", json=body)
        assert r.status_code == 201, r.text
        contractors[body["full_name"]] = r.json()["id"]

    run = demo_client.post("/runs", json={"employer_id": emp["id"],
                                          "lines": [{"contractor_id": cid} for cid in contractors.values()]}).json()
    assert run["status"] == "RESERVED"
    assert run["assessment"]["payroll_currency"] == "MIXED"
    by_ccy = {r["currency"]: r for r in run["assessment"]["by_currency"]}
    assert by_ccy["EUR"]["required"] == "1012.85"   # 1000 salary + 12.85 SWIFT fee
    assert by_ccy["USD"]["required"] == "500.00"    # LOCAL: no payout fee
    assert by_ccy["GBP"]["required"] == "400.00"    # LOCAL: no payout fee
    # payroll_usd = 1012.85*1.08 + 500 + 400*1.27 = 1093.88 + 500 + 508 = 2101.88
    assert run["assessment"]["payroll_usd"] == "2101.88"

    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED"
    assert [p["status"] for p in done["pay_lines"]] == ["PAID", "PAID", "PAID"]
    fees = {p["beneficiary_name"]: p["payout_fee"] for p in done["pay_lines"]}
    assert fees == {"Joris de Vries": "12.85", "Sam Lee": "0.00", "Priya Shah": "0.00"}
    # 20000 - 2101.88 converted/paid - 149 run fee
    assert _balances(demo_client, "Delta Foods BV") == {"USD": "17749.12"}
    assert done["receipt"] is not None


# ---------------------------------------------------------------------------
# Partial contractor list: run for a subset only
# ---------------------------------------------------------------------------

def test_partial_contractor_list_pays_only_the_subset(demo_client):
    acme = _detail(demo_client, ACME)
    tomas = next(x for x in acme["contractors"] if x["full_name"] == "Tomas Rivera")
    done = _run_and_approve(demo_client, acme["id"], [{"contractor_id": tomas["id"]}])

    assert len(done["pay_lines"]) == 1
    assert done["pay_lines"][0]["beneficiary_name"] == "Tomas Rivera"
    assert done["pay_lines"][0]["status"] == "PAID"
    # Only Tomas's leg left the wallet: (2600 + 12.85) * 1.08 = 2821.88, + 149 fee.
    assert _balances(demo_client, ACME) == {"USD": "17029.12"}

    # Lena was not paid by this run: no transfer exists for her.
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        from app.db.models import AirwallexRequest

        transfers = [r for r in s.query(AirwallexRequest).all() if r.operation == "create_transfer"]
    assert len(transfers) == 1


# ---------------------------------------------------------------------------
# Mid-run deposit does not change a held employer's outcome
# ---------------------------------------------------------------------------

def test_mid_run_deposits_do_not_change_held_outcome(demo_client):
    borealis = _detail(demo_client, BOREALIS)
    line = [{"contractor_id": borealis["contractors"][0]["id"]}]
    run = demo_client.post("/runs", json={"employer_id": borealis["id"], "lines": line}).json()
    assert run["status"] == "HELD_SHORTFALL" and run["shortfall_usd"] == "2642.88"

    # A deposit lands in ANOTHER employer's silo mid-run...
    cobalt_id = _employers(demo_client)[COBALT]["id"]
    demo_client.post(f"/employers/{cobalt_id}/fund", json={"amount": "10000"})
    # ...and even a deposit into Borealis's OWN wallet mid-run does not
    # rewrite the held run's priced assessment.
    demo_client.post(f"/employers/{borealis['id']}/fund", json={"amount": "1000"})
    assert _balances(demo_client, BOREALIS) == {"USD": "6000.00"}

    # The held run can still only be held; approving it is a conflict.
    assert demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).status_code == 409
    held = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "hold"}).json()
    assert held["status"] == "HELD_SHORTFALL" and held["fee_charged_usd"] == "0.00"

    # A NEW run re-prices against the new balance: shortfall drops by the
    # deposit amount, but the run is still held (needs 7,642.88 total).
    fresh = demo_client.post("/runs", json={"employer_id": borealis["id"], "lines": line}).json()
    assert fresh["status"] == "HELD_SHORTFALL" and fresh["shortfall_usd"] == "1642.88"
    assert demo_client.get("/overview").json()["platform_fees"] == {}


# ---------------------------------------------------------------------------
# Payout failure + retry succeeds without double-paying
# ---------------------------------------------------------------------------

class _FailSecondTransition:
    """Wraps the simulator: the SECOND transfer's PAID transition raises
    after the inner state already changed (a lost response), so the first
    payout is fully complete when the run fails mid-batch."""

    def __init__(self, inner):
        self.inner = inner
        self.transitions = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def transition_transfer(self, **kwargs):
        result = self.inner.transition_transfer(**kwargs)
        self.transitions += 1
        if self.transitions == 2:
            raise RuntimeError("simulated transient failure on second payout transition")
        return result


def test_payout_failure_then_resume_pays_exactly_once():
    from app.agents.demo import EMPLOYERS, build_demo_services, demo_resume
    from app.agents.graph import Services, build_graph, initial_state, resume_failed
    from app.airwallex.simulator import USD_VALUE, AirwallexSimulator
    from app.ledger import Ledger
    from app.partners.metal import MockSettlementChain

    base = build_demo_services()
    services = Services(airwallex=_FailSecondTransition(AirwallexSimulator()), ledger=Ledger(),
                        chain=MockSettlementChain(), usd_rates=dict(USD_VALUE), tap=base.tap)
    graph = build_graph(services)
    config = {"configurable": {"thread_id": "run-retry"}}
    result = graph.invoke(
        initial_state(run_id="run-retry", employer_key="acme", employer_name="Acme Manufacturing GmbH",
                      funding_usd="20000", pay_lines=EMPLOYERS["acme"]["pay_lines"]), config)
    assert result["__interrupt__"][0].value["kind"] == "approval"
    state = graph.get_state(config).values
    graph.invoke(Command(resume=demo_resume(services, state, result["__interrupt__"][0].value)), config)

    failed = graph.get_state(config).values
    assert failed["status"] == "FAILED"
    assert failed["completed_payouts"] == [0]  # first payout done, second not

    resume_failed(graph, config)
    final = graph.get_state(config).values
    assert final["status"] == "SETTLED"
    # Golden balance: paid exactly once despite the mid-batch failure.
    assert Decimal(final["balances"]["USD"]) == Decimal("13343.24")
    assert services.ledger.is_balanced()
    # Exactly two transfers exist in the simulator — no duplicate payout.
    assert len(services.airwallex.inner._transfers) == 2
    # Resuming a settled run is refused, so a stray retry cannot re-pay.
    try:
        resume_failed(graph, config)
        raise AssertionError("resume_failed on a SETTLED run must raise")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Restart while paused (file-backed checkpointer)
# ---------------------------------------------------------------------------

def test_restart_while_paused_via_chat_started_run(tmp_path):
    """Start a run, restart the whole app (new runtime, same DB file +
    same checkpoint file), then approve: the pause survived and the run
    settles with the single-contractor golden balance."""
    from app.api import create_app
    from app.config import Settings
    from app.db import Database
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    url = f"sqlite:///{(tmp_path / 'paysilo.db').as_posix()}"
    settings = Settings(database_url=url, jwt_secret="scenario-secret-" + "x" * 32,
                        checkpoint_path=str(tmp_path / "checkpoints.sqlite"))
    login = {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}

    with TestClient(create_app(settings, Database.from_url(url))) as c:
        c.post("/auth/login", json=login)
        acme = next(e for e in c.get("/employers").json() if e["name"].startswith("Acme"))
        detail = c.get(f"/employers/{acme['id']}").json()
        lena = detail["contractors"][0]
        run = c.post("/runs", json={"employer_id": acme["id"],
                                    "lines": [{"contractor_id": lena["id"]}]}).json()
        assert run["status"] == "RESERVED" and run["resumable"] is True

    with TestClient(create_app(settings, Database.from_url(url))) as c:  # the "restart"
        c.post("/auth/login", json=login)
        paused = c.get(f"/runs/{run['id']}").json()
        assert paused["status"] == "RESERVED" and paused["resumable"] is True
        assert paused["pending"]["kind"] == "approval"
        done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
        assert done["status"] == "SETTLED", done
        # Lena only: (3400 + 12.85) * 1.08 = 3685.88 converted, + 149 fee.
        acme = next(e for e in c.get("/employers").json() if e["name"].startswith("Acme"))
        assert acme["balances"] == {"USD": "16165.12"}
