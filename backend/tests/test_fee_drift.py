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


def test_run_spends_currency_already_held_before_converting_usd():
    # Seen live: an earlier failed run left EUR and GBP in the wallet. Affordability counted
    # them, but execute converted the FULL amount from USD and ran out of USD mid-run.
    base = build_demo_services()
    sim = AirwallexSimulator()
    services = Services(airwallex=sim, ledger=Ledger(), chain=MockSettlementChain(),
                        usd_rates=dict(USD_VALUE), tap=base.tap)
    acct = sim.create_connected_account(request_id="held-acct", business_name="Acme")["id"]
    for ccy, amount in (("USD", "9260.74"), ("EUR", "4811.81"), ("GBP", "2100.00")):
        sim.deposit(request_id=f"held-{ccy}", account_id=acct, amount=Decimal(amount), currency=ccy)
        services.ledger.post([(f"acme:cash:{ccy}", Decimal(amount)), (f"external:bank:{ccy}", -Decimal(amount))],
                             memo=f"leftover {ccy}")
    lines = [
        {"name": "Aryan Talikoti", "amount": "3200", "currency": "EUR", "method": "SWIFT",
         "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "country_code": "DE"},
        LINES[1], LINES[2],
        {"name": "Sam Lee", "amount": "1800", "currency": "USD", "method": "LOCAL",
         "account_number": "50001121", "routing_code": "021000021", "country_code": "US"},
        {"name": "Olivia Brown", "amount": "2100", "currency": "GBP", "method": "LOCAL",
         "account_number": "55779911", "routing_code": "200000", "country_code": "GB"},
    ]
    graph = build_graph(services)
    config = {"configurable": {"thread_id": "held-1"}}
    result = graph.invoke(initial_state(run_id="held-1", employer_key="acme", employer_name="Acme",
                                        funding_usd="0", pay_lines=lines, account_id=acct), config)
    assert result["__interrupt__"][0].value["kind"] == "approval"
    state = graph.get_state(config).values
    graph.invoke(Command(resume=demo_resume(services, state, result["__interrupt__"][0].value)), config)

    final = graph.get_state(config).values
    assert final["status"] == "SETTLED", final.get("error")
    messages = " | ".join(e["message"] for e in final["events"])
    # EUR needed 8025.70, held 4811.81 -> buys only 3213.89; GBP fully covered by holdings.
    assert "EUR 3213.89" in messages and "on top of EUR 4811.81 already held" in messages
    assert "Using GBP 2100.00 already held" in messages
    # 9260.74 - 3471.00 (EUR gap at 1.08) - 1800 (Sam) - 149 fee
    assert Decimal(final["balances"]["USD"]) == Decimal("3840.74")
    assert "EUR" not in final["balances"] and "GBP" not in final["balances"]  # leftovers fully used
    assert services.ledger.is_balanced()
