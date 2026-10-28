from decimal import Decimal

import pytest

from app.policy import ContractorPayroll, RefusalError, assess_payroll

RATES = {"USD": Decimal("1"), "EUR": Decimal("1.08")}
ACME = [
    ContractorPayroll("Lena Fischer", Decimal("3400"), "EUR"),
    ContractorPayroll("Tomas Rivera", Decimal("2600"), "EUR"),
]
BOREALIS = [ContractorPayroll("Ingrid Solberg", Decimal("6000"), "EUR")]


def test_fee_first_total_acme():
    a = assess_payroll(ACME, RATES, {"USD": Decimal("20000")})
    assert a.total_payroll == Decimal("6000.00")
    assert a.swift_fees == Decimal("25.70")  # 2 x EUR 12.85, priced BEFORE FX
    assert a.total_required == Decimal("6025.70")
    assert a.payroll_usd == Decimal("6507.76")
    assert a.platform_fee_usd == Decimal("149.00")
    assert a.required_usd == Decimal("6656.76")  # what actually leaves the wallet
    assert a.available_usd == Decimal("20000.00")
    assert a.can_run is True
    assert a.shortfall_usd == Decimal("0.00")


def test_borealis_shortfall_math():
    a = assess_payroll(BOREALIS, RATES, {"USD": Decimal("5000")})
    assert a.total_required == Decimal("6012.85")
    assert a.payroll_usd == Decimal("6493.88")
    assert a.required_usd == Decimal("6642.88")
    assert a.available_usd == Decimal("5000.00")
    # 6,642.88 out + 1,000 reserve that must stay = 7,642.88 needed vs 5,000.
    assert a.shortfall_usd == Decimal("2642.88")
    assert a.can_run is False


def test_unknown_currency_rate_raises_refusal():
    with pytest.raises(RefusalError):
        assess_payroll(ACME, {"USD": Decimal("1")}, {"USD": Decimal("20000")})


def test_unknown_wallet_currency_raises_refusal():
    with pytest.raises(RefusalError):
        assess_payroll(ACME, RATES, {"USD": Decimal("20000"), "GBP": Decimal("50")})


def test_run_fee_and_reserve_floor_are_part_of_affordability():
    # Exactly enough for salaries + SWIFT fees, nothing for the $149 run fee
    # or the $1,000 reserve: must NOT run (previously it ran, paid everyone,
    # then failed to collect the fee).
    lines = [ContractorPayroll("A", Decimal("3000"), "EUR")]
    a = assess_payroll(lines, RATES, {"USD": Decimal("3253.88")})
    assert a.payroll_usd == Decimal("3253.88")
    assert a.can_run is False
    assert a.shortfall_usd == Decimal("1149.00")


def test_exactly_enough_including_fee_and_reserve_can_run():
    lines = [ContractorPayroll("A", Decimal("3000"), "EUR")]
    a = assess_payroll(lines, RATES, {"USD": Decimal("4402.88")})  # 3253.88 + 149 + 1000
    assert a.can_run is True
    assert a.shortfall_usd == Decimal("0.00")


def test_unknown_payout_fee_is_refused_not_guessed():
    with pytest.raises(RefusalError):
        assess_payroll([ContractorPayroll("A", Decimal("100"), "GBP", "SWIFT")],
                       {**RATES, "GBP": Decimal("1.27")}, {"USD": Decimal("20000")})


def test_mixed_currency_payroll_priced_per_currency():
    lines = [ContractorPayroll("Lena", Decimal("1000"), "EUR", "SWIFT"),
             ContractorPayroll("Sam", Decimal("500"), "USD", "LOCAL")]
    a = assess_payroll(lines, RATES, {"USD": Decimal("20000")})
    assert a.payroll_currency == "MIXED"
    by = {r.currency: r for r in a.by_currency}
    assert by["EUR"].required == Decimal("1012.85")
    assert by["EUR"].required_usd == Decimal("1093.88")
    assert by["USD"].required == Decimal("500.00")
    assert a.payroll_usd == Decimal("1593.88")
    assert a.required_usd == Decimal("1742.88")


def test_non_positive_amount_refused():
    with pytest.raises(RefusalError):
        assess_payroll([ContractorPayroll("A", Decimal("0"), "EUR")], RATES, {"USD": Decimal("20000")})


def test_empty_payroll_cannot_run_and_charges_nothing():
    a = assess_payroll([], RATES, {"USD": Decimal("20000")})
    assert a.can_run is False
    assert a.platform_fee_usd == Decimal("0.00")
