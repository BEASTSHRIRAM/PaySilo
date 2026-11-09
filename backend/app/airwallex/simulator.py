"""In-memory Airwallex simulator.

Implements the same interface as the real REST client so the full payroll
graph runs end-to-end with NO API keys. Models the sandbox behaviours the
Kit 6 scenario depends on:

- connected accounts with strictly separate wallets,
- simulation deposits,
- FX quotes/conversions at the demo rate (1 EUR = 1.08 USD),
- beneficiaries and transfers (a transfer debits the salary plus the payout
  fee from the policy schedule — e.g. EUR 12.85 per EUR SWIFT payout — which
  is exactly why policy prices the fee BEFORE converting),
- platform charges (debit an employer, credit the platform wallet),
- **idempotency**: every mutating call takes a ``request_id``; a repeated
  ``request_id`` returns the original result and never moves money twice.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from decimal import Decimal

from ..money import round_money, to_decimal
from ..policy import PAYROLL_POLICY

# Demo FX: USD value of one unit of each currency. 1 EUR = 1.08 USD is the
# Starter Kit 6 rate; GBP is included so multi-currency payroll can be demoed.
USD_VALUE: dict[str, Decimal] = {"USD": Decimal("1"), "EUR": Decimal("1.08"), "GBP": Decimal("1.27")}

PLATFORM_ACCOUNT_ID = "platform"


def request_id_for(run_id: str, operation: str) -> str:
    """Deterministic request_id per (run, operation) — retries reuse it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"paysilo:{run_id}:{operation}"))


class SimulatorError(Exception):
    pass


class AirwallexSimulator:
    def __init__(self, usd_value: dict[str, Decimal] | None = None):
        self.usd_value = dict(usd_value or USD_VALUE)
        self._accounts: dict[str, dict] = {
            PLATFORM_ACCOUNT_ID: {"name": "PaySilo Platform", "status": "ACTIVE", "balances": {}}
        }
        self._idempotency: dict[str, dict] = {}
        self._quotes: dict[str, dict] = {}
        self._beneficiaries: dict[str, dict] = {}
        self._transfers: dict[str, dict] = {}
        self._seq = itertools.count(1)

    # -- internal helpers -------------------------------------------------
    def _once(self, request_id: str, fn) -> dict:
        if request_id in self._idempotency:
            return copy.deepcopy(self._idempotency[request_id])
        result = fn()
        self._idempotency[request_id] = copy.deepcopy(result)
        return copy.deepcopy(result)

    def _account(self, account_id: str) -> dict:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise SimulatorError(f"Unknown account {account_id}") from None

    def _credit(self, account_id: str, currency: str, amount: Decimal) -> None:
        balances = self._account(account_id)["balances"]
        balances[currency] = round_money(balances.get(currency, Decimal("0")) + amount)

    def _debit(self, account_id: str, currency: str, amount: Decimal) -> None:
        balances = self._account(account_id)["balances"]
        current = balances.get(currency, Decimal("0"))
        if current < amount:
            raise SimulatorError(
                f"Insufficient funds in {account_id}: {currency} {current} < {amount}"
            )
        balances[currency] = round_money(current - amount)

    # -- connected accounts ------------------------------------------------
    def create_connected_account(self, *, request_id: str, business_name: str, contact_name: str = "",
                                 email: str = "") -> dict:
        def _create() -> dict:
            account_id = f"acct_{next(self._seq):03d}_{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex[:8]}"
            self._accounts[account_id] = {
                "name": business_name,
                "contact": contact_name,
                "status": "ACTIVE",
                "balances": {},
            }
            return {"id": account_id, "business_name": business_name, "status": "ACTIVE"}

        return self._once(request_id, _create)

    def get_connected_account(self, *, account_id: str) -> dict:
        acct = self._account(account_id)
        return {"id": account_id, "business_name": acct["name"], "status": acct["status"]}

    def create_hosted_onboarding_link(self, *, account_id: str, template: str, return_url: str = "") -> dict:
        self._account(account_id)
        # Demo mode needs no onboarding: simulator accounts start ACTIVE.
        return {"id": f"hosted_{account_id}", "account_id": account_id,
                "status": "NOT_REQUIRED", "url": ""}

    def authorize_hosted_onboarding_link(self, *, flow_id: str) -> dict:
        return {"id": flow_id, "status": "NOT_REQUIRED", "url": ""}

    def activate_connected_account(self, *, account_id: str) -> dict:
        acct = self._account(account_id)
        acct["status"] = "ACTIVE"
        return {"id": account_id, "business_name": acct["name"], "status": "ACTIVE"}

    def restore_account(self, account_id: str, *, business_name: str,
                        balances: dict[str, Decimal] | None = None) -> None:
        """Rehydrate an account (e.g. from the persisted ledger after a restart)."""
        self._accounts[account_id] = {
            "name": business_name, "contact": "", "status": "ACTIVE",
            "balances": {c: round_money(to_decimal(v)) for c, v in (balances or {}).items()},
        }

    def get_balances(self, *, account_id: str | None = None) -> dict[str, Decimal]:
        acct = self._account(account_id or PLATFORM_ACCOUNT_ID)
        return {c: round_money(v) for c, v in acct["balances"].items() if v != 0}

    def balance_of(self, currency: str, *, account_id: str | None = None) -> Decimal:
        return self.get_balances(account_id=account_id).get(currency, Decimal("0.00"))

    # -- funding -----------------------------------------------------------
    def deposit(self, *, request_id: str, account_id: str, amount: object, currency: str = "USD") -> dict:
        amount = round_money(to_decimal(amount))

        def _deposit() -> dict:
            self._credit(account_id, currency, amount)
            return {"id": f"dep_{next(self._seq):04d}", "account_id": account_id,
                    "amount": amount, "currency": currency, "status": "SETTLED"}

        return self._once(request_id, _deposit)

    # -- FX ----------------------------------------------------------------
    def create_fx_quote(self, *, request_id: str, on_behalf_of: str, sell_currency: str,
                        buy_currency: str, buy_amount: object) -> dict:
        buy_amount = round_money(to_decimal(buy_amount))

        def _quote() -> dict:
            # We quote the USD value of 1 unit of buy currency (1 EUR = 1.08 USD).
            usd_per_unit = self.usd_value[buy_currency]
            quote = {
                "id": f"quote_{next(self._seq):04d}",
                "on_behalf_of": on_behalf_of,
                "sell_currency": sell_currency,
                "buy_currency": buy_currency,
                "buy_amount": buy_amount,
                "rate": usd_per_unit,  # USD per 1 unit of buy currency
                "status": "QUOTED",
            }
            self._quotes[quote["id"]] = quote
            return quote

        return self._once(request_id, _quote)

    def create_fx_conversion(self, *, request_id: str, on_behalf_of: str, quote_id: str,
                             buy_amount: object, sell_currency: str = "USD", buy_currency: str = "EUR") -> dict:
        buy_amount = round_money(to_decimal(buy_amount))

        def _convert() -> dict:
            quote = self._quotes.get(quote_id)
            if quote is None:
                raise SimulatorError(f"Unknown FX quote {quote_id}")
            sell_amount = round_money(buy_amount * to_decimal(quote["rate"]))
            self._debit(on_behalf_of, sell_currency, sell_amount)
            self._credit(on_behalf_of, buy_currency, buy_amount)
            return {"id": f"conv_{next(self._seq):04d}", "quote_id": quote_id,
                    "on_behalf_of": on_behalf_of, "sell_amount": sell_amount,
                    "sell_currency": sell_currency, "buy_amount": buy_amount,
                    "buy_currency": buy_currency, "status": "SETTLED"}

        return self._once(request_id, _convert)

    # -- beneficiaries & transfers ------------------------------------------
    def create_beneficiary(self, *, request_id: str, on_behalf_of: str, name: str,
                           currency: str = "EUR", country_code: str = "DE", method: str = "SWIFT",
                           iban: str = "", swift_code: str = "", account_number: str = "",
                           routing_code: str = "") -> dict:
        def _create() -> dict:
            self._account(on_behalf_of)
            if not (iban or account_number):
                raise SimulatorError(f"Beneficiary {name} has no IBAN or account number")
            bene = {"id": f"bene_{next(self._seq):04d}", "on_behalf_of": on_behalf_of,
                    "name": name, "currency": currency, "country_code": country_code,
                    "method": method, "iban": iban, "swift_code": swift_code,
                    "account_number": account_number, "status": "APPROVED"}
            self._beneficiaries[bene["id"]] = bene
            return bene

        return self._once(request_id, _create)

    def create_transfer(self, *, request_id: str, on_behalf_of: str, beneficiary_id: str,
                        amount: object, currency: str = "EUR", method: str = "SWIFT") -> dict:
        amount = round_money(to_decimal(amount))

        def _transfer() -> dict:
            bene = self._beneficiaries.get(beneficiary_id)
            if bene is None:
                raise SimulatorError(f"Unknown beneficiary {beneficiary_id}")
            if bene["on_behalf_of"] != on_behalf_of:
                raise SimulatorError("Beneficiary belongs to a different connected account")
            fee = PAYROLL_POLICY.payout_fee(currency, method)
            self._debit(on_behalf_of, currency, round_money(amount + fee))
            transfer = {"id": f"trf_{next(self._seq):04d}", "on_behalf_of": on_behalf_of,
                        "beneficiary_id": beneficiary_id, "amount": amount, "currency": currency,
                        "method": method, "fee_amount": fee, "status": "SUBMITTED"}
            self._transfers[transfer["id"]] = transfer
            return transfer

        return self._once(request_id, _transfer)

    def transition_transfer(self, *, request_id: str, on_behalf_of: str, transfer_id: str,
                            target_status: str = "PAID") -> dict:
        def _transition() -> dict:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                raise SimulatorError(f"Unknown transfer {transfer_id}")
            transfer["status"] = target_status
            return dict(transfer)

        return self._once(request_id, _transition)

    # -- platform charges ----------------------------------------------------
    def create_charge(self, *, request_id: str, source_account_id: str, amount: object,
                      currency: str = "USD", reference: str = "") -> dict:
        amount = round_money(to_decimal(amount))

        def _charge() -> dict:
            self._debit(source_account_id, currency, amount)
            self._credit(PLATFORM_ACCOUNT_ID, currency, amount)
            return {"id": f"chg_{next(self._seq):04d}", "source": source_account_id,
                    "amount": amount, "currency": currency, "reference": reference,
                    "status": "SUCCEEDED"}

        return self._once(request_id, _charge)
