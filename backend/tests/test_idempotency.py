from decimal import Decimal

from app.airwallex.simulator import AirwallexSimulator, request_id_for


def _funded_sim():
    sim = AirwallexSimulator()
    acct = sim.create_connected_account(request_id="acct-1", business_name="Acme Manufacturing GmbH")
    sim.deposit(request_id="fund-1", account_id=acct["id"], amount=Decimal("20000"))
    return sim, acct["id"]


def test_transfer_idempotent_on_eur_wallet():
    sim = AirwallexSimulator()
    acct = sim.create_connected_account(request_id="acct-1", business_name="Acme Manufacturing GmbH")
    sim.deposit(request_id="fund-eur", account_id=acct["id"], amount=Decimal("6025.70"), currency="EUR")
    bene = sim.create_beneficiary(request_id="bene-1", on_behalf_of=acct["id"], name="Lena Fischer")
    rid = request_id_for("run-1", "pay-0")
    first = sim.create_transfer(request_id=rid, on_behalf_of=acct["id"],
                                beneficiary_id=bene["id"], amount=Decimal("3400"))
    after_first = sim.balance_of("EUR", account_id=acct["id"])
    second = sim.create_transfer(request_id=rid, on_behalf_of=acct["id"],
                                 beneficiary_id=bene["id"], amount=Decimal("3400"))
    after_second = sim.balance_of("EUR", account_id=acct["id"])
    assert second["id"] == first["id"]          # original result returned
    assert after_second == after_first           # money moved exactly once
    assert after_first == Decimal("6025.70") - Decimal("3400") - Decimal("12.85")


def test_charge_idempotent():
    sim, acct_id = _funded_sim()
    rid = request_id_for("run-1", "platform-fee")
    first = sim.create_charge(request_id=rid, source_account_id=acct_id, amount=Decimal("149"))
    second = sim.create_charge(request_id=rid, source_account_id=acct_id, amount=Decimal("149"))
    assert second["id"] == first["id"]
    assert sim.balance_of("USD") == Decimal("149.00")  # platform credited once


def test_request_id_is_deterministic():
    assert request_id_for("run-1", "pay-0") == request_id_for("run-1", "pay-0")
    assert request_id_for("run-1", "pay-0") != request_id_for("run-1", "pay-1")
