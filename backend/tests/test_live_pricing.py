"""Graph pricing in live mode: rates come from the live provider or the
run is refused — the demo table is never used as a fallback."""

from __future__ import annotations

from decimal import Decimal

from app.agents.graph import Services, build_graph, initial_state
from app.airwallex.rates import LiveRateUnavailable
from app.airwallex.simulator import USD_VALUE, AirwallexSimulator
from app.ledger import Ledger
from app.partners.metal import MockSettlementChain

PAY_LINES = [
    {"name": "Lena Fischer", "amount": "3400", "currency": "EUR", "method": "SWIFT",
     "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "country_code": "DE"},
]


class _FixedProvider:
    def rates_for(self, currencies, *, account_id, run_id):
        return {"USD": Decimal("1"), "EUR": Decimal("1.08")}


class _RefusingProvider:
    def rates_for(self, currencies, *, account_id, run_id):
        raise LiveRateUnavailable("Live FX rate for EUR is unavailable in this test.")


def _run(provider):
    services = Services(
        airwallex=AirwallexSimulator(), ledger=Ledger(), chain=MockSettlementChain(),
        usd_rates=dict(USD_VALUE), rate_provider=provider,
    )
    graph = build_graph(services)
    state = initial_state(run_id="live-pricing-1", employer_key="acme",
                          employer_name="Acme Manufacturing GmbH", funding_usd="20000",
                          pay_lines=PAY_LINES)
    return graph.invoke(state, {"configurable": {"thread_id": "live-pricing-1"}})


def test_graph_prices_with_live_provider_rates():
    result = _run(_FixedProvider())
    # Priced and reserved, paused at the TAP approval interrupt.
    assert result["status"] == "RESERVED"
    assert result["assessment"]["total_required"] == "3412.85"
    assert "__interrupt__" in result


def test_graph_refuses_when_live_rate_unavailable():
    result = _run(_RefusingProvider())
    assert result["status"] == "REFUSED"
    assert "Live FX rate for EUR is unavailable" in result["error"]


class _RecordingProvider:
    def __init__(self):
        self.asked: set[str] = set()

    def rates_for(self, currencies, *, account_id, run_id):
        self.asked |= set(currencies)
        return {"USD": Decimal("1"), "EUR": Decimal("1.08")}


def test_rates_are_only_requested_for_payroll_and_held_currencies(monkeypatch):
    # The live wallet lists every currency, mostly at zero (seen live: 46 entries).
    # Quoting a zero-balance currency like ARS failed with amount_below_limit and
    # refused the whole run, so those must never be asked for.
    provider = _RecordingProvider()
    real = AirwallexSimulator.get_balances

    def with_zero_currencies(self, *, account_id=None):
        balances = real(self, account_id=account_id)
        return {**balances, "ARS": Decimal("0"), "JPY": Decimal("0.00")}

    monkeypatch.setattr(AirwallexSimulator, "get_balances", with_zero_currencies)
    result = _run(provider)
    assert result["status"] == "RESERVED"
    assert provider.asked == {"EUR", "USD"}
