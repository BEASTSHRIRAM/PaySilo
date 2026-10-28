"""In-memory double-entry ledger.

Rules (enforced in code, not by convention):

- Accounts are namespaced: ``"<namespace>:<account>:<CCY>"`` where the
  namespace is an employer key, ``platform``, or ``external``.
- Every transaction's entries must sum to zero **per currency**.
- A transaction may touch at most ONE employer namespace. ``platform`` and
  ``external`` are shared counterparty namespaces (fees out, funding in).
  A transaction spanning two employers is rejected — cross-employer money
  movement is not merely discouraged, it is unrepresentable.
- Balances are derived from entries only. There is no setter for a balance.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from .money import round_money, to_decimal

SHARED_NAMESPACES = {"platform", "external"}


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    account: str  # "<namespace>:<account>:<CCY>"
    amount: Decimal  # signed; positive = value into the account


@dataclass
class LedgerTransaction:
    id: str
    memo: str
    entries: list[LedgerEntry]


def _namespace(account: str) -> str:
    return account.split(":", 1)[0]


def _currency(account: str) -> str:
    return account.rsplit(":", 1)[-1]


@dataclass
class Ledger:
    transactions: list[LedgerTransaction] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)

    def post(self, entries: list[tuple[str, object]], memo: str = "") -> LedgerTransaction:
        """Append a balanced transaction or raise :class:`LedgerError`."""
        built = [LedgerEntry(account=a, amount=round_money(to_decimal(v))) for a, v in entries]
        built = [e for e in built if e.amount != 0]  # a zero leg (e.g. a free LOCAL payout fee) carries no value
        if not built:
            raise LedgerError("Transaction has no non-zero entries")

        employer_namespaces = {
            _namespace(e.account) for e in built if _namespace(e.account) not in SHARED_NAMESPACES
        }
        if len(employer_namespaces) > 1:
            raise LedgerError(
                f"Cross-employer transaction rejected: namespaces {sorted(employer_namespaces)} "
                "in one transaction. Funds are tenant-scoped."
            )

        per_currency: dict[str, Decimal] = {}
        for e in built:
            per_currency[_currency(e.account)] = per_currency.get(_currency(e.account), Decimal("0")) + e.amount
        unbalanced = {c: v for c, v in per_currency.items() if v != 0}
        if unbalanced:
            raise LedgerError(f"Transaction does not balance per currency: {unbalanced}")

        txn = LedgerTransaction(id=f"txn-{next(self._ids):04d}", memo=memo, entries=built)
        self.transactions.append(txn)
        return txn

    def balance(self, account: str) -> Decimal:
        """Balance derived from entries only."""
        total = sum((e.amount for t in self.transactions for e in t.entries if e.account == account), Decimal("0"))
        return round_money(total)

    def employer_cash(self, employer_key: str, currency: str) -> Decimal:
        return self.balance(f"{employer_key}:cash:{currency}")

    def is_balanced(self) -> bool:
        """Global trial balance: every currency sums to zero across all accounts."""
        per_currency: dict[str, Decimal] = {}
        for t in self.transactions:
            for e in t.entries:
                per_currency[_currency(e.account)] = per_currency.get(_currency(e.account), Decimal("0")) + e.amount
        return all(v == 0 for v in per_currency.values())
