"""Payroll policy — PaySilo's fee-first payroll assessment engine.

Design rule: the model reads, the code decides, the API moves the money.
Everything in this module is deterministic code. No LLM is consulted for
arithmetic, fees, or affordability — ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import round_money, to_decimal


class RefusalError(Exception):
    """Raised when policy refuses to assess rather than guess.

    PaySilo refuses when a USD rate is missing: an unknown currency
    rate must never be silently treated as 1:1 with people's salaries.
    """


@dataclass(frozen=True)
class PayrollPolicy:
    swift_fee_eur: Decimal = Decimal("12.85")
    platform_fee_per_run_usd: Decimal = Decimal("149")
    reserve_floor_usd: Decimal = Decimal("1000")


PAYROLL_POLICY = PayrollPolicy()


@dataclass(frozen=True)
class ContractorPayroll:
    name: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PayrollAssessment:
    total_payroll: Decimal
    swift_fees: Decimal
    total_required: Decimal
    available_usd: Decimal
    required_usd: Decimal
    can_run: bool
    shortfall_usd: Decimal


def assess_payroll(
    contractors: list[ContractorPayroll],
    usd_value: dict[str, Decimal],
    employer_balance: dict[str, Decimal],
    policy: PayrollPolicy = PAYROLL_POLICY,
) -> PayrollAssessment:
    """Price payroll including one SWIFT fee per EUR payout BEFORE converting.

    ``usd_value`` gives the USD value of one unit of each currency. An
    unknown currency raises :class:`RefusalError` — we refuse to assess
    affordability rather than guess at 1:1.

    Only ``employer_balance`` — that employer's own wallet — is consulted.
    There is deliberately no parameter for "platform total": the aggregated
    balance illusion has nowhere to live in this signature.
    """
    total_payroll = round_money(sum((to_decimal(c.amount) for c in contractors), Decimal("0")))
    eur_payouts = sum(1 for c in contractors if c.currency == "EUR")
    swift_fees = round_money(Decimal(eur_payouts) * policy.swift_fee_eur)
    total_required = round_money(total_payroll + swift_fees)

    payroll_currency = contractors[0].currency if contractors else "EUR"
    payroll_rate = usd_value.get(payroll_currency)
    if payroll_rate is None:
        raise RefusalError(
            f"No USD rate supplied for payroll currency {payroll_currency}; "
            "refusing to assess affordability."
        )
    required_usd = round_money(total_required * to_decimal(payroll_rate))

    available = Decimal("0")
    for currency, amount in employer_balance.items():
        amount = to_decimal(amount)
        if amount == 0:
            continue
        rate = usd_value.get(currency)
        if rate is None:
            raise RefusalError(
                f"No USD rate supplied for {currency} in the employer wallet; "
                "refusing to assess affordability."
            )
        available += amount * to_decimal(rate)
    available_usd = round_money(available)

    shortfall_usd = round_money(max(Decimal("0"), required_usd - available_usd))
    return PayrollAssessment(
        total_payroll=total_payroll,
        swift_fees=swift_fees,
        total_required=total_required,
        available_usd=available_usd,
        required_usd=required_usd,
        can_run=shortfall_usd == 0,
        shortfall_usd=shortfall_usd,
    )


def cross_tenant_guard(short_employer: str, other_employer: str) -> str:
    """Tenant isolation message recorded when a cross-tenant draw is refused.

    Another employer's deposit, even one that arrives mid-run, must never
    fund this employer's payroll. The assessment already uses only the
    employer's own wallet; this guard records why a short employer is
    escalated instead of raided.
    """
    return (
        f"Refusing to draw on {other_employer}'s wallet for {short_employer}: "
        "funds are tenant-scoped and the platform only executes with the "
        "source account's authorization."
    )
