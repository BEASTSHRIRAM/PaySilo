"""Live FX rate provider — pricing rates from real Airwallex FX quotes.

Demo mode prices from the static demo table (``simulator.USD_VALUE``).
Live mode must never do that: a guessed rate misprices salaries. This
provider fetches a fresh FX quote per currency from the employer's own
connected account (``x-on-behalf-of``) and derives the USD value of one
unit of the currency from the quote itself:

- when the quote carries ``sell_amount`` / ``buy_amount`` (a USD -> CCY
  quote), USD per unit is ``sell_amount / buy_amount`` — unambiguous;
- otherwise a ``rate`` field is read with the simulator's semantics
  (USD per one unit of the buy currency);
- otherwise ``client_rate`` / ``exchange_rate`` (buy currency per one USD)
  is inverted.

Any failure — API error, missing fields, a non-positive rate — raises
:class:`LiveRateUnavailable`, a policy :class:`RefusalError`. Callers must
refuse the run; there is deliberately no fallback to the demo table.
"""

from __future__ import annotations

from decimal import Decimal

from ..money import to_decimal
from ..policy import RefusalError
from .client import AirwallexError
from .simulator import request_id_for

# Probe size for rate quotes: large enough that the sell amount is not
# distorted by cent rounding, small enough to stay a quote (no conversion
# is created, so no money moves).
PROBE_BUY_AMOUNT = Decimal("100")


class LiveRateUnavailable(RefusalError):
    """A live FX rate could not be obtained; pricing must refuse."""


def usd_per_unit_from_quote(quote: dict, currency: str) -> Decimal:
    """Derive the USD value of one unit of ``currency`` from a quote."""
    sell, buy = quote.get("sell_amount"), quote.get("buy_amount")
    if sell is not None and buy is not None:
        buy_d = to_decimal(str(buy))
        if buy_d > 0:
            rate = to_decimal(str(sell)) / buy_d
            if rate > 0:
                return rate
    if quote.get("rate") is not None:
        rate = to_decimal(str(quote["rate"]))
        if rate > 0:
            return rate
    for field in ("client_rate", "exchange_rate"):
        if quote.get(field) is not None:
            quoted = to_decimal(str(quote[field]))
            if quoted > 0:
                return Decimal("1") / quoted
    raise LiveRateUnavailable(
        f"Live FX quote for {currency} carried no usable rate or amounts; "
        "refusing to price this payroll rather than guess the rate."
    )


class LiveFxRateProvider:
    """Fetches ``usd_rates`` for the graph's pricing step in live mode.

    ``client`` is anything with the Airwallex interface (the REST client,
    usually behind the recording wrapper). Quotes are requested on the
    employer's own behalf with a deterministic ``request_id`` per
    (run, currency), so re-pricing a run reuses the original quote.
    """

    def __init__(self, client: object, *, probe_buy_amount: Decimal = PROBE_BUY_AMOUNT):
        self.client = client
        self.probe_buy_amount = probe_buy_amount

    def rates_for(self, currencies: set[str] | list[str] | tuple[str, ...], *,
                  account_id: str | None, run_id: str) -> dict[str, Decimal]:
        if not account_id:
            raise LiveRateUnavailable(
                "Live FX pricing requires the employer's connected account id; "
                "refusing to price this payroll without one."
            )
        rates: dict[str, Decimal] = {"USD": Decimal("1")}
        for currency in sorted({str(c).upper() for c in currencies} - {"USD"}):
            try:
                quote = self.client.create_fx_quote(  # type: ignore[attr-defined]
                    request_id=request_id_for(run_id, f"fx-rate-{currency}"),
                    on_behalf_of=account_id,
                    sell_currency="USD", buy_currency=currency,
                    buy_amount=self.probe_buy_amount,
                )
            except AirwallexError as exc:
                raise LiveRateUnavailable(
                    f"Live FX rate for {currency} is unavailable ({exc}); "
                    "refusing to price this payroll rather than use a demo rate."
                ) from exc
            rates[currency] = usd_per_unit_from_quote(quote, currency)
        return rates
