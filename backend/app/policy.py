"""Payroll policy — PaySilo's fee-first payroll assessment engine.

Design rule: the model reads, the code decides, the API moves the money.
Everything in this module is deterministic code. No LLM is consulted for
arithmetic, fees, or affordability — ever.

What "can this employer run payroll?" means, precisely:

    payroll_usd  = sum over currencies of (salaries + payout fees) x USD rate
    required_usd = payroll_usd + platform run fee        (what leaves the wallet)
    needed_usd   = required_usd + reserve floor           (what must be there)
    can_run      = employer's OWN wallet (in USD) >= needed_usd

Payout fees are priced BEFORE any conversion, per currency and payout
method. A currency with no USD rate, or a (currency, method) pair with no
fee schedule, is a refusal — never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .money import round_money, to_decimal

ZERO = Decimal("0.00")


class RefusalError(Exception):
    """Raised when policy refuses to assess rather than guess.

    An unknown currency rate or an unknown payout fee must never be
    silently treated as 1:1 or as free when people's salaries are at stake.
    """


def _default_payout_fees() -> dict[tuple[str, str], Decimal]:
    # (currency, method) -> fee per payout, charged in that currency.
    # EUR SWIFT is the Starter Kit 6 schedule. LOCAL rails carry no payout
    # fee in the sandbox. Anything else must be added explicitly.
    return {
        ("EUR", "SWIFT"): Decimal("12.85"),
        ("EUR", "LOCAL"): Decimal("0.00"),
        ("USD", "LOCAL"): Decimal("0.00"),
        ("GBP", "LOCAL"): Decimal("0.00"),
    }


@dataclass(frozen=True)
class PayrollPolicy:
    platform_fee_per_run_usd: Decimal = Decimal("149")
    reserve_floor_usd: Decimal = Decimal("1000")
    payout_fees: dict[tuple[str, str], Decimal] = field(default_factory=_default_payout_fees)

    @property
    def swift_fee_eur(self) -> Decimal:
        return self.payout_fees[("EUR", "SWIFT")]

    def payout_fee(self, currency: str, method: str) -> Decimal:
        key = (currency.upper(), method.upper())
        if key not in self.payout_fees:
            raise RefusalError(
                f"No payout fee schedule for {key[0]} via {key[1]}; refusing to price this "
                "payroll rather than guess the fee."
            )
        return self.payout_fees[key]


PAYROLL_POLICY = PayrollPolicy()


@dataclass(frozen=True)
class ContractorPayroll:
    name: str
    amount: Decimal
    currency: str
    method: str = "SWIFT"
    # PARTIAL settlement order: lower pays first (ties: smallest line first). Never affects pricing.
    priority: int = 100


@dataclass(frozen=True)
class CurrencyRequirement:
    currency: str
    payouts: int
    payroll: Decimal  # salaries, in ``currency``
    fees: Decimal  # payout fees, in ``currency``
    required: Decimal  # payroll + fees, in ``currency``
    required_usd: Decimal


@dataclass(frozen=True)
class PayrollAssessment:
    payroll_currency: str  # the single payroll currency, or "MIXED"
    by_currency: tuple[CurrencyRequirement, ...]
    # Totals in ``payroll_currency``; USD-equivalents when the run is MIXED.
    total_payroll: Decimal
    swift_fees: Decimal  # all payout fees (named for the Kit 6 SWIFT schedule)
    total_required: Decimal
    payroll_usd: Decimal  # salaries + payout fees, in USD
    platform_fee_usd: Decimal
    reserve_floor_usd: Decimal
    required_usd: Decimal  # payroll_usd + platform fee: what leaves the wallet
    available_usd: Decimal
    shortfall_usd: Decimal  # vs required_usd + reserve floor
    can_run: bool
    # True when the run fee was already collected on the original payroll (a
    # continuation run): ``platform_fee_usd`` is then zero by design.
    run_fee_waived: bool = False


def _rate(usd_value: dict[str, Decimal], currency: str, where: str) -> Decimal:
    rate = usd_value.get(currency)
    if rate is None:
        raise RefusalError(
            f"No USD rate supplied for {currency} {where}; refusing to assess affordability."
        )
    return to_decimal(rate)


def assess_payroll(
    contractors: list[ContractorPayroll],
    usd_value: dict[str, Decimal],
    employer_balance: dict[str, Decimal],
    policy: PayrollPolicy = PAYROLL_POLICY,
    waive_run_fee: bool = False,
) -> PayrollAssessment:
    """Price payroll fees-first and decide affordability from ONE wallet.

    ``waive_run_fee`` is for a continuation run whose payroll already paid the
    run fee: the fee is neither priced nor required again (the reserve floor
    and payout fees still are).

    Only ``employer_balance`` — that employer's own wallet — is consulted.
    There is deliberately no parameter for "platform total": the aggregated
    balance illusion has nowhere to live in this signature.
    """
    for c in contractors:
        if to_decimal(c.amount) <= 0:
            raise RefusalError(f"Pay line for {c.name} must be a positive amount.")

    groups: dict[str, list[ContractorPayroll]] = {}
    for c in contractors:
        groups.setdefault(c.currency.upper(), []).append(c)

    requirements: list[CurrencyRequirement] = []
    for currency, lines in groups.items():
        payroll = round_money(sum((to_decimal(l.amount) for l in lines), Decimal("0")))
        fees = round_money(sum((policy.payout_fee(currency, l.method) for l in lines), Decimal("0")))
        required = round_money(payroll + fees)
        rate = _rate(usd_value, currency, "for payroll")
        requirements.append(CurrencyRequirement(
            currency=currency, payouts=len(lines), payroll=payroll, fees=fees,
            required=required, required_usd=round_money(required * rate),
        ))

    payroll_usd = round_money(sum((r.required_usd for r in requirements), Decimal("0")))
    if len(requirements) == 1:
        only = requirements[0]
        payroll_currency, total_payroll, swift_fees, total_required = (
            only.currency, only.payroll, only.fees, only.required)
    elif not requirements:
        payroll_currency, total_payroll, swift_fees, total_required = "USD", ZERO, ZERO, ZERO
    else:
        usd_rates = {r.currency: _rate(usd_value, r.currency, "for payroll") for r in requirements}
        payroll_currency = "MIXED"
        total_payroll = round_money(sum((r.payroll * usd_rates[r.currency] for r in requirements), Decimal("0")))
        swift_fees = round_money(sum((r.fees * usd_rates[r.currency] for r in requirements), Decimal("0")))
        total_required = payroll_usd

    available = Decimal("0")
    for currency, amount in employer_balance.items():
        amount = to_decimal(amount)
        if amount == 0:
            continue
        available += amount * _rate(usd_value, currency, "in the employer wallet")
    available_usd = round_money(available)

    platform_fee = round_money(policy.platform_fee_per_run_usd) if requirements and not waive_run_fee else ZERO
    required_usd = round_money(payroll_usd + platform_fee)
    needed_usd = round_money(required_usd + policy.reserve_floor_usd) if requirements else ZERO
    shortfall_usd = round_money(max(Decimal("0"), needed_usd - available_usd))
    return PayrollAssessment(
        payroll_currency=payroll_currency,
        by_currency=tuple(requirements),
        total_payroll=total_payroll,
        swift_fees=swift_fees,
        total_required=total_required,
        payroll_usd=payroll_usd,
        platform_fee_usd=platform_fee,
        reserve_floor_usd=round_money(policy.reserve_floor_usd),
        required_usd=required_usd,
        available_usd=available_usd,
        shortfall_usd=shortfall_usd,
        can_run=bool(requirements) and shortfall_usd == 0,
        run_fee_waived=bool(waive_run_fee),
    )


@dataclass(frozen=True)
class PartialPayoutPlan:
    """Which pay lines a short employer's OWN wallet can still fund.

    ``paid`` / ``held`` are indices into the contractor list given to
    :func:`plan_partial_payout`. ``assessment`` prices only the paid lines
    (fee-first, with the run fee and reserve floor); ``full`` is the
    assessment of the whole batch, kept for the exact shortfall.
    """

    paid: tuple[int, ...]
    held: tuple[int, ...]
    assessment: PayrollAssessment
    full: PayrollAssessment


def plan_partial_payout(
    contractors: list[ContractorPayroll],
    usd_value: dict[str, Decimal],
    employer_balance: dict[str, Decimal],
    policy: PayrollPolicy = PAYROLL_POLICY,
    waive_run_fee: bool = False,
) -> PartialPayoutPlan | None:
    """Kit 6 shortfall rule: pay what the employer's own balance supports.

    Lines are walked in contractor ``priority`` order (lower first), ties
    smallest first by USD-equivalent (amount + that line's payout fee).
    A line is paid when ``payroll_usd + run fee + reserve floor <=
    available`` still holds with it; an unaffordable line is held and the
    walk goes on (a smaller, lower-priority line can still fit). Returns ``None`` when the whole
    batch is affordable (nothing to split) or when not even the smallest
    line fits (hold everything). Only ``employer_balance`` is consulted.
    """
    full = assess_payroll(contractors, usd_value, employer_balance, policy, waive_run_fee=waive_run_fee)
    if full.can_run or not contractors:
        return None
    cost: dict[int, Decimal] = {}
    for i, c in enumerate(contractors):
        ccy = c.currency.upper()
        line_total = to_decimal(c.amount) + policy.payout_fee(ccy, c.method)
        cost[i] = line_total * _rate(usd_value, ccy, "for payroll")
    order = sorted(range(len(contractors)), key=lambda i: (contractors[i].priority, cost[i], i))
    run_fee = ZERO if waive_run_fee else policy.platform_fee_per_run_usd
    budget = full.available_usd - run_fee - policy.reserve_floor_usd
    chosen: list[int] = []
    spent = Decimal("0")
    for i in order:
        if spent + cost[i] > budget:
            continue
        chosen.append(i)
        spent += cost[i]
    # Per-currency rounding can differ by a cent from the sum of line costs:
    # verify with the real assessment and drop the largest line until it fits.
    while chosen:
        paid = sorted(chosen)
        assessment = assess_payroll([contractors[i] for i in paid], usd_value, employer_balance, policy,
                                    waive_run_fee=waive_run_fee)
        if assessment.can_run:
            held = tuple(i for i in range(len(contractors)) if i not in paid)
            return PartialPayoutPlan(paid=tuple(paid), held=held, assessment=assessment, full=full)
        chosen.pop()
    return None


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
