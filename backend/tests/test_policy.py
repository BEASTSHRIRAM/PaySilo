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
    assert a.required_usd == Decimal("6507.76")
    assert a.available_usd == Decimal("20000.00")
    assert a.can_run is True
    assert a.shortfall_usd == Decimal("0.00")


def test_borealis_shortfall_math():
    a = assess_payroll(BOREALIS, RATES, {"USD": Decimal("5000")})
    assert a.total_required == Decimal("6012.85")
    assert a.required_usd == Decimal("6493.88")
    assert a.available_usd == Decimal("5000.00")
    assert a.shortfall_usd == Decimal("1493.88")
    assert a.can_run is False


def test_unknown_currency_rate_raises_refusal():
    with pytest.raises(RefusalError):
        assess_payroll(ACME, {"USD": Decimal("1")}, {"USD": Decimal("20000")})


def test_unknown_wallet_currency_raises_refusal():
    with pytest.raises(RefusalError):
        assess_payroll(ACME, RATES, {"USD": Decimal("20000"), "GBP": Decimal("50")})
