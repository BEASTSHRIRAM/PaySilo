"""Real payout fees drift above the priced fee (live sandbox: ~EUR 13.38 vs 12.85 per
SWIFT payout). The FX & Payout agent must top up from the employer's OWN USD when a
payout is refused for insufficient balance, then retry the same request_id — and never
top up for a payout that was already sent."""

from decimal import Decimal

from langgraph.types import Command

from app.agents.demo import build_demo_services, demo_resume
from app.agents.graph import Services, build_graph, initial_state
from app.airwallex.simulator import USD_VALUE, AirwallexSimulator
from app.ledger import Ledger
from app.partners.metal import MockSettlementChain

REAL_SWIFT_FEE = Decimal("13.39")
LINES = [
    {"name": "Lena Fischer", "amount": "3400", "currency": "EUR", "method": "SWIFT",
     "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "country_code": "DE"},
    {"name": "Tomas Rivera", "amount": "2600", "currency": "EUR", "method": "SWIFT",
     "iban": "ES9121000418450200051332", "swift_code": "CAIXESBBXXX", "country_code": "ES"},
    {"name": "Noor van Dijk", "amount": "2200", "currency": "EUR", "method": "LOCAL",
     "iban": "NL91ABNA0417164300", "swift_code": "ABNANL2A", "country_code": "NL"},
]


class _DriftingFees:
    """Simulator wrapper: EUR SWIFT payouts cost 13.39 (as live), not the priced 12.85."""

    def __init__(self, inner: AirwallexSimulator):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def create_transfer(self, **kwargs):
        before = len(self.inner._transfers)
        transfer = self.inner.create_transfer(**kwargs)
        is_new = len(self.inner._transfers) > before
        if kwargs.get("currency") == "EUR" and kwargs.get("method") == "SWIFT":
            extra = REAL_SWIFT_FEE - Decimal(str(transfer["fee_amount"]))
            if is_new and extra > 0:
                self.inner._debit(kwargs["on_behalf_of"], "EUR", extra)
            transfer = {**transfer, "fee_amount": REAL_SWIFT_FEE}
        return transfer


def test_fee_drift_tops_up_from_own_usd_and_settles():
    base = build_demo_services()
    services = Services(airwallex=_DriftingFees(AirwallexSimulator()), ledger=Ledger(),
                        chain=MockSettlementChain(), usd_rates=dict(USD_VALUE), tap=base.tap)
    graph = build_graph(services)
    config = {"configurable": {"thread_id": "drift-1"}}
    result = graph.invoke(initial_state(run_id="drift-1", employer_key="acme", employer_name="Acme",
                                        funding_usd="25000", pay_lines=LINES), config)
    state = graph.get_state(config).values
    graph.invoke(Command(resume=demo_resume(services, state, result["__interrupt__"][0].value)), config)

    final = graph.get_state(config).values
    assert final["status"] == "SETTLED", final.get("error")
    assert final["completed_payouts"] == [0, 1, 2]
    topped = [e for e in final["events"] if "Topped up EUR" in e["message"]]
    assert len(topped) == 1  # only the payout that was actually short
    assert "no other employer's money" in topped[0]["message"]
    assert len(services.airwallex.inner._transfers) == 3  # nobody paid twice
    assert services.ledger.is_balanced()  # top-up posted like any FX conversion
