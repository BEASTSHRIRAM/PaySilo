"""Money helpers: Decimal everywhere, round-half-up to 2dp.

Money is never a float in PaySilo. Every amount that crosses a module
boundary is a ``Decimal`` quantized to cents with ROUND_HALF_UP, matching
our ``round2`` behaviour.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value: object) -> Decimal:
    """Convert int/str/Decimal to Decimal without float contamination."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):  # defensive: go through str, never binary float math
        return Decimal(str(value))
    return Decimal(value)  # type: ignore[arg-type]


def round_money(value: object) -> Decimal:
    """Round to 2 decimal places, half-up (our ``round2``)."""
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: object) -> str:
    """Stable string form for state/API payloads, e.g. ``'6025.70'``."""
    return format(round_money(value), "f")
