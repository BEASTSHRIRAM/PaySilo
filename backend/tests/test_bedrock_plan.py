"""The pricing plan cross check: same figures from the plan graph, and how the pricing node treats the result."""

from decimal import Decimal

from app.agents.cloud_plan import plan_payload, run_plan
from app.agents.demo import build_demo_services
from app.agents.graph import assessment_dict
from app.partners.agentcore import agentcore_handler
from app.policy import ContractorPayroll, assess_payroll

LINES = [ContractorPayroll("Lena Fischer", Decimal("3400"), "EUR", "SWIFT"),
         ContractorPayroll("Sam Lee", Decimal("2600"), "USD", "LOCAL", priority=5)]
RATES = {"USD": Decimal("1"), "EUR": Decimal("1.08")}


def _local(balances, waive=False):
    return assessment_dict(assess_payroll(LINES, RATES, balances, waive_run_fee=waive))


def test_plan_graph_matches_the_backend_assessment():
    for balances, waive in (({"USD": Decimal("20000")}, False), ({"USD": Decimal("900")}, False),
                            ({"USD": Decimal("20000"), "EUR": Decimal("500")}, True)):
        out = run_plan(plan_payload(LINES, RATES, balances, waive))
        assert out["graph"] == "paysilo.plan" and out["assessment"] == _local(balances, waive)


def test_plan_payload_carries_no_names_or_bank_details():
    payload = plan_payload(LINES, RATES, {"USD": Decimal("20000")}, False)
    assert "Lena" not in str(payload) and "Sam" not in str(payload)
    assert set(payload["lines"][0]) == {"amount", "currency", "method", "priority"}


def test_agentcore_handler_plan_action():
    payload = plan_payload(LINES, RATES, {"USD": Decimal("20000")}, False)
    out = agentcore_handler({"action": "plan", "plan": payload})
    assert out["action"] == "plan" and out["assessment"] == _local({"USD": Decimal("20000")})
    assert "error" in agentcore_handler({"action": "nope"})


class _Planner:
    region = "ap-southeast-2"

    def __init__(self, mode):
        self.mode, self.calls = mode, 0

    def plan(self, payload):
        self.calls += 1
        if self.mode == "down":
            raise TimeoutError("no route")
        out = run_plan(payload)
        if self.mode == "wrong":
            out["assessment"] = {**out["assessment"], "required_usd": "1.00"}
        return {**out, "region": self.region, "session_id": "paysilo-plan-0123456789abcdef", "ms": 42}


def _demo(mode):
    """Run the whole Kit 6 demo scenario with a fake planner in place of Bedrock."""
    from app.agents.demo import run_demo

    services = build_demo_services()
    planner = _Planner(mode)
    services.cloud_planner = planner
    return planner, run_demo(services)


def _bedrock_lines(report):
    return [line for line in report["audit"] if "Bedrock AgentCore" in line]


def test_matching_plan_is_recorded_and_the_scenario_is_unchanged():
    planner, report = _demo("match")
    assert planner.calls >= 2  # Acme and Borealis are both priced
    assert any("match" in line for line in _bedrock_lines(report))
    assert report["employers"]["acme"]["status"] == "SETTLED"
    assert report["employers"]["borealis"]["status"] == "HELD_SHORTFALL"
    assert report["platform"]["fees_collected_usd"] == "149.00"


def test_a_disagreeing_plan_refuses_the_run_and_moves_no_money():
    _, report = _demo("wrong")
    assert report["employers"]["acme"]["status"] == "REFUSED"
    assert any("does not match" in line for line in _bedrock_lines(report))
    assert report["platform"]["fees_collected_usd"] == "0.00"


def test_an_unreachable_runtime_does_not_block_payroll():
    planner, report = _demo("down")
    assert planner.calls >= 1
    assert any("could not be reached" in line for line in _bedrock_lines(report))
    assert report["employers"]["acme"]["status"] == "SETTLED"
