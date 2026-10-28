from decimal import Decimal

import pytest

from app.ledger import Ledger, LedgerError


def test_entries_sum_to_zero_and_balance_derived():
    ledger = Ledger()
    ledger.post([("acme:cash:USD", Decimal("20000")), ("external:bank:USD", Decimal("-20000"))],
                memo="funding")
    assert ledger.balance("acme:cash:USD") == Decimal("20000.00")
    assert ledger.employer_cash("acme", "USD") == Decimal("20000.00")
    assert ledger.is_balanced() is True


def test_unbalanced_transaction_rejected():
    ledger = Ledger()
    with pytest.raises(LedgerError):
        ledger.post([("acme:cash:USD", Decimal("100"))], memo="one-legged")


def test_cross_employer_transaction_rejected():
    ledger = Ledger()
    with pytest.raises(LedgerError):
        ledger.post([("acme:cash:USD", Decimal("-100")), ("borealis:cash:USD", Decimal("100"))],
                    memo="the forbidden move")


def test_platform_counterparty_allowed():
    ledger = Ledger()
    ledger.post([("acme:cash:USD", Decimal("20000")), ("external:bank:USD", Decimal("-20000"))])
    ledger.post([("acme:cash:USD", Decimal("-149")), ("platform:fees:USD", Decimal("149"))])
    assert ledger.balance("platform:fees:USD") == Decimal("149.00")
    assert ledger.is_balanced() is True
