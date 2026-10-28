"""Real Airwallex sandbox REST client (httpx).

Same interface as :class:`AirwallexSimulator`. Not exercised in tests (no
keys in CI/demo), but complete: token auth, ``x-on-behalf-of`` on every
employer-scoped call, and a caller-supplied deterministic ``request_id``
on every mutating call so a retry can never move money twice (Airwallex
deduplicates on ``request_id``; a 409 means "already done", never retry
blindly).

Endpoints follow the Airwallex builder guide for Starter Kit 6:
connected accounts, global accounts, simulation deposits, beneficiaries,
FX quotes/conversions, transfers, simulation transfer transitions, charges.
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx

from ..config import Settings, load_settings
from ..money import round_money, to_decimal


class AirwallexError(Exception):
    pass


class AirwallexClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings or load_settings()
        self._http = httpx.Client(
            base_url=self.settings.airwallex_base_url.rstrip("/"),
            timeout=30.0,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth ---------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        if not (self.settings.airwallex_client_id and self.settings.airwallex_api_key):
            raise AirwallexError(
                "AIRWALLEX_CLIENT_ID / AIRWALLEX_API_KEY not set. "
                "Add them to .env (see .env.example) or run in demo mode."
            )
        resp = self._http.post(
            "/authentication/login",
            headers={
                "x-client-id": self.settings.airwallex_client_id,
                "x-api-key": self.settings.airwallex_api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        self._token_expiry = time.time() + 25 * 60  # tokens last ~30 min
        return self._token

    def _request(self, method: str, path: str, *, on_behalf_of: str | None = None,
                 json: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if on_behalf_of:
            headers["x-on-behalf-of"] = on_behalf_of
        resp = self._http.request(method, path, headers=headers, json=json)
        if resp.status_code == 409:
            raise AirwallexError(
                f"409 conflict on {path}: this request_id was already processed. "
                "Do not retry with a new id — fetch the original result instead."
            )
        resp.raise_for_status()
        return resp.json()

    # -- connected accounts ---------------------------------------------------
    def create_connected_account(self, *, request_id: str, business_name: str, contact_name: str = "") -> dict:
        return self._request(
            "POST", "/connected_accounts/create",
            json={"request_id": request_id, "business_name": business_name,
                  "contact_name": contact_name},
        )

    def get_balances(self, *, account_id: str | None = None) -> dict[str, Decimal]:
        data = self._request("GET", "/balances/current", on_behalf_of=account_id)
        out: dict[str, Decimal] = {}
        for line in data.get("items", data if isinstance(data, list) else []):
            out[line["currency"]] = round_money(to_decimal(line["available_amount"]))
        return out

    def balance_of(self, currency: str, *, account_id: str | None = None) -> Decimal:
        return self.get_balances(account_id=account_id).get(currency, Decimal("0.00"))

    # -- funding (sandbox simulation) ------------------------------------------
    def deposit(self, *, request_id: str, account_id: str, amount: object, currency: str = "USD") -> dict:
        return self._request(
            "POST", "/simulation/deposit/create", on_behalf_of=account_id,
            json={"request_id": request_id, "amount": str(round_money(to_decimal(amount))),
                  "currency": currency},
        )

    # -- FX ---------------------------------------------------------------------
    def create_fx_quote(self, *, request_id: str, on_behalf_of: str, sell_currency: str,
                        buy_currency: str, buy_amount: object) -> dict:
        return self._request(
            "POST", "/fx/quotes/create", on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "sell_currency": sell_currency,
                  "buy_currency": buy_currency,
                  "buy_amount": str(round_money(to_decimal(buy_amount)))},
        )

    def create_fx_conversion(self, *, request_id: str, on_behalf_of: str, quote_id: str,
                             buy_amount: object, sell_currency: str = "USD", buy_currency: str = "EUR") -> dict:
        return self._request(
            "POST", "/fx/conversions/create", on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "quote_id": quote_id,
                  "sell_currency": sell_currency, "buy_currency": buy_currency,
                  "buy_amount": str(round_money(to_decimal(buy_amount)))},
        )

    # -- beneficiaries & transfers ----------------------------------------------
    def create_beneficiary(self, *, request_id: str, on_behalf_of: str, name: str,
                           currency: str = "EUR", country_code: str = "DE", method: str = "SWIFT",
                           iban: str = "", swift_code: str = "", account_number: str = "",
                           routing_code: str = "") -> dict:
        bank_account: dict = {"account_name": name, "account_currency": currency,
                              "bank_country_code": country_code}
        if iban:
            bank_account["iban"] = iban
        if swift_code:
            bank_account["swift_code"] = swift_code
        if account_number:
            bank_account["account_number"] = account_number
        if routing_code:
            bank_account["account_routing_type1"] = "aba" if currency == "USD" else "sort_code"
            bank_account["account_routing_value1"] = routing_code
        payload = {"request_id": request_id,
                   "beneficiary": {"entity_type": "PERSONAL", "bank_details": bank_account},
                   "transfer_methods": [method]}
        self._request("POST", "/beneficiaries/validate", on_behalf_of=on_behalf_of, json=payload)
        return self._request("POST", "/beneficiaries/create", on_behalf_of=on_behalf_of, json=payload)

    def create_transfer(self, *, request_id: str, on_behalf_of: str, beneficiary_id: str,
                        amount: object, currency: str = "EUR", method: str = "SWIFT") -> dict:
        return self._request(
            "POST", "/transfers/create", on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "beneficiary_id": beneficiary_id,
                  "transfer_amount": str(round_money(to_decimal(amount))),
                  "transfer_currency": currency, "transfer_method": method,
                  "reason": "professional_services"},
        )

    def transition_transfer(self, *, request_id: str, on_behalf_of: str, transfer_id: str,
                            target_status: str = "PAID") -> dict:
        return self._request(
            "POST", f"/simulation/transfers/{transfer_id}/transition",
            on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "target_status": target_status},
        )

    # -- platform charges ---------------------------------------------------------
    def create_charge(self, *, request_id: str, source_account_id: str, amount: object,
                      currency: str = "USD", reference: str = "") -> dict:
        return self._request(
            "POST", "/charges/create",
            json={"request_id": request_id, "source_id": source_account_id,
                  "amount": str(round_money(to_decimal(amount))), "currency": currency,
                  "reason": "fee", "reference": reference},
        )
