from decimal import Decimal

from app.airwallex.simulator import USD_VALUE, AirwallexSimulator
from app.policy import ContractorPayroll, assess_payroll, cross_tenant_guard

RATES = dict(USD_VALUE)
BOREALIS = [ContractorPayroll("Ingrid Solberg", Decimal("6000"), "EUR")]


def test_cobalt_deposit_does_not_change_borealis_assessment():
    sim = AirwallexSimulator()
    borealis = sim.create_connected_account(request_id="b-acct", business_name="Borealis Labs AB")
    cobalt = sim.create_connected_account(request_id="c-acct", business_name="Cobalt Retail LLC")
    sim.deposit(request_id="b-fund", account_id=borealis["id"], amount=Decimal("5000"))

    before = assess_payroll(BOREALIS, RATES, sim.get_balances(account_id=borealis["id"]))

    # Cobalt's USD 10,000 deposit lands mid-run.
    sim.deposit(request_id="c-deposit", account_id=cobalt["id"], amount=Decimal("10000"))
    assert sim.balance_of("USD", account_id=cobalt["id"]) == Decimal("10000.00")

    after = assess_payroll(BOREALIS, RATES, sim.get_balances(account_id=borealis["id"]))
    assert after == before
    assert after.can_run is False
    assert after.shortfall_usd == Decimal("1493.88")


def test_guard_message():
    msg = cross_tenant_guard("Borealis Labs AB", "Cobalt Retail LLC")
    assert "Refusing to draw on Cobalt Retail LLC's wallet for Borealis Labs AB" in msg
    assert "tenant-scoped" in msg
