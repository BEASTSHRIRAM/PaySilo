"""Real Airwallex sandbox REST client (httpx).

Same interface as :class:`AirwallexSimulator`. Token auth, ``x-on-behalf-of``
on every employer-scoped call, and a caller-supplied deterministic
``request_id`` on every mutating call so a retry can never move money twice.

Call shapes were verified against the Airwallex API flows this codebase
implements (Starter Kit 6: connected accounts, hosted onboarding flows,
simulation deposits and account activation, balances, beneficiaries, FX
quotes/conversions, transfers, simulation transfer transitions, charges):

- ``POST /authentication/login`` with ``x-client-id`` / ``x-api-key``
  headers; the returned token is cached until shortly before expiry.
- Connected accounts are created platform-side (no ``x-on-behalf-of`` —
  there is no tenant yet); every later employer-scoped call carries it.
- Hosted onboarding is ``POST /hosted_flows/create`` (``account_id`` +
  ``template`` + ``return_url``) followed by
  ``POST /hosted_flows/{id}/authorize``; in the sandbox an account is then
  forced to ``ACTIVE`` with
  ``POST /simulation/accounts/{id}/update_status``
  (``{"next_status": "ACTIVE", "force": true}``) before any FX or transfer.
- Transfers are paid from the wallet that already holds the transfer
  currency (FX is an explicit, separate step in PaySilo), so each transfer
  carries ``source_currency`` equal to the transfer currency and
  ``fee_paid_by: "PAYER"`` — the payer bears the payout fee, exactly as the
  policy prices it before conversion.
- The platform run fee uses ``POST /charges/create`` against the single
  source employer account — see ``backend/docs/airwallex-fee-path.md``.

Duplicate handling: Airwallex signals a reused ``request_id`` with HTTP
409, or — for charges and connected-account transfers — with an error body
whose ``code`` is ``request_id_duplicate`` / ``duplicate_request_id`` and
whose ``details.source_id`` identifies the original resource. Either way
this client fetches and returns the original resource instead of failing
or retrying with a new id.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import httpx

from ..config import Settings, load_settings
from ..money import round_money, to_decimal

DUPLICATE_CODES = {"request_id_duplicate", "duplicate_request_id"}


class AirwallexError(Exception):
    """An Airwallex call failed; the provider's code/message are preserved."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _body_json(resp: httpx.Response) -> dict | list | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, (dict, list)) else None


def _is_duplicate(status_code: int, body: dict | list | None) -> bool:
    if status_code == 409:
        return True
    return isinstance(body, dict) and body.get("code") in DUPLICATE_CODES


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

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _retry_after(resp: httpx.Response, tries: int) -> float:
        header = resp.headers.get("Retry-After")
        try:
            if header is not None:
                return min(max(float(header), 0.0), 60.0)
        except ValueError:
            pass
        return min(0.5 * (2 ** (tries - 1)), 30.0)

    @staticmethod
    def _is_unconfigured_fee(resp: httpx.Response) -> bool:
        body = _body_json(resp)
        return isinstance(body, dict) and body.get("code") == "unconfigured_client_fee"

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
        if resp.status_code >= 400:
            raise AirwallexError(
                f"Airwallex authentication failed (HTTP {resp.status_code}).",
                status_code=resp.status_code,
            )
        data = resp.json()
        self._token = data["token"]
        self._token_expiry = time.time() + 25 * 60  # tokens last ~30 min
        return self._token

    # -- request plumbing -----------------------------------------------------
    def _headers(self, on_behalf_of: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if on_behalf_of:
            headers["x-on-behalf-of"] = on_behalf_of
        return headers

    def _get(self, path: str, *, on_behalf_of: str | None = None,
             params: dict | None = None) -> dict:
        resp = self._http.request("GET", path, headers=self._headers(on_behalf_of), params=params)
        if resp.status_code >= 400:
            body = _body_json(resp)
            code = body.get("code") if isinstance(body, dict) else None
            raise AirwallexError(
                f"GET {path} failed (HTTP {resp.status_code}, code={code}).",
                status_code=resp.status_code, code=code,
            )
        data = _body_json(resp)
        return data if isinstance(data, dict) else {}

    def _recover_duplicate(self, body: dict | list | None, *, on_behalf_of: str | None,
                           request_id: str | None, duplicate_item_path: str | None,
                           duplicate_collection: str | None) -> dict | None:
        """Fetch the original resource for a duplicate ``request_id``.

        Order: a resource embedded in the error body, the item identified
        by ``details.source_id``, then the collection filtered by
        ``request_id``. Returns ``None`` when nothing can be fetched.
        """
        if isinstance(body, dict) and "id" in body and "code" not in body:
            return body
        details = body.get("details") if isinstance(body, dict) else None
        source_id = None
        if isinstance(details, dict):
            source_id = details.get("source_id") or details.get("id")
        if not source_id and isinstance(body, dict):
            source_id = body.get("source_id")
        if source_id and duplicate_item_path:
            try:
                return self._get(duplicate_item_path.format(id=source_id),
                                 on_behalf_of=on_behalf_of)
            except AirwallexError:
                pass  # fall through to the collection lookup
        if request_id and duplicate_collection:
            try:
                data = self._get(duplicate_collection, on_behalf_of=on_behalf_of,
                                 params={"request_id": request_id})
            except AirwallexError:
                return None
            items = data.get("items") if isinstance(data, dict) else None
            # Only accept an item that really carries OUR request_id; the list
            # filter is not guaranteed, and a wrong item must never be adopted.
            for item in items or []:
                if isinstance(item, dict) and item.get("request_id") == request_id:
                    return item
            if isinstance(data, dict) and "id" in data and data.get("request_id") == request_id:
                return data
        return None

    def _request(self, method: str, path: str, *, on_behalf_of: str | None = None,
                 json: dict | None = None, params: dict | None = None,
                 request_id: str | None = None, duplicate_item_path: str | None = None,
                 duplicate_collection: str | None = None) -> dict | list:
        """One API call, with idempotent retries on transient failures.

        A retry re-sends the *identical* payload, including the caller's
        deterministic ``request_id`` — Airwallex deduplicates on it, so a
        retry after a timeout or a 5xx can never move money twice. A
        duplicate response is never retried: the original resource is
        fetched and returned instead (see ``_recover_duplicate``).
        """
        attempts = 3 if method.upper() == "POST" else 1
        resp: httpx.Response | None = None
        rate_limit_tries = 0
        fee_wait_total = 0.0
        fee_delay = 5.0
        attempt = -1
        while True:
            attempt += 1
            try:
                resp = self._http.request(method, path, headers=self._headers(on_behalf_of),
                                          json=json, params=params)
            except httpx.TransportError as exc:  # connection dropped / timed out: transient
                if attempt + 1 < attempts:
                    time.sleep(0.1 * (2 ** attempt))
                    continue
                raise AirwallexError(
                    f"Airwallex request to {path} failed after {attempts} attempts: {exc}"
                ) from exc
            if resp.status_code == 429:  # sandbox rate limit: back off, same request_id
                rate_limit_tries += 1
                if rate_limit_tries <= self.settings.airwallex_rate_limit_retries:
                    attempt -= 1  # a 429 does not consume a transport/5xx attempt
                    self._sleep(self._retry_after(resp, rate_limit_tries))
                    continue
            elif resp.status_code >= 400 and self._is_unconfigured_fee(resp):
                # A freshly activated account may take minutes to get its fee
                # configuration. Bounded retry with the identical request_id.
                budget = float(self.settings.airwallex_fee_config_retry_seconds)
                if fee_wait_total < budget:
                    delay = min(fee_delay, budget - fee_wait_total)
                    fee_wait_total += delay
                    fee_delay = min(fee_delay * 2, 30.0)
                    attempt -= 1
                    self._sleep(delay)
                    continue
            if resp.status_code >= 500 and attempt + 1 < attempts:  # transient server error
                time.sleep(0.1 * (2 ** attempt))
                continue
            break
        assert resp is not None  # the loop always sets it or raises
        if resp.status_code < 300:
            data = _body_json(resp)
            return data if data is not None else {}
        body = _body_json(resp)
        if _is_duplicate(resp.status_code, body):
            recovered = self._recover_duplicate(
                body, on_behalf_of=on_behalf_of, request_id=request_id,
                duplicate_item_path=duplicate_item_path,
                duplicate_collection=duplicate_collection)
            if recovered is not None:
                return recovered
            raise AirwallexError(
                f"Duplicate request_id on {path}: the original request was already "
                "processed (or is still being processed) and could not be fetched. "
                "Do not retry with a new id — fetch the original result instead.",
                status_code=resp.status_code,
                code=body.get("code") if isinstance(body, dict) else None,
            )
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        raise AirwallexError(
            f"{method} {path} failed (HTTP {resp.status_code}"
            + (f", code={code}" if code else "")
            + (f"): {message}" if message else ")."),
            status_code=resp.status_code, code=code,
        )

    # -- connected accounts & onboarding ---------------------------------------
    def create_connected_account(self, *, request_id: str, business_name: str, contact_name: str = "",
                                 ein: str = "", country_code: str = "US", email: str = "",
                                 confirm_wait_seconds: float = 2.0) -> dict:
        """Create AND activate an employer's connected account (Kit 6 flow).

        1. ``POST /accounts/create`` (platform-scoped) -> ``acct_...`` CREATED
        2. ``POST /accounts/{id}/update`` with an EIN in
           ``account_details.business_details.business_identifiers`` (an
           account cannot be edited after submit)
        3. ``POST /accounts/{id}/submit`` (empty body)
        4. ``POST /simulation/accounts/{id}/update_status``
           ``{"next_status": "ACTIVE", "force": true}`` (sandbox)
        5. ``GET /accounts/{id}`` — must report ACTIVE, else we raise.

        Returns ``{"id", "status", "business_name", ...}``. If the employer
        has no EIN on file, a clearly sandbox-only placeholder derived from
        ``request_id`` is used (deterministic, so a retry sends the same one).
        """
        if not ein:
            digits = "".join(c for c in str(int(uuid.uuid5(uuid.NAMESPACE_URL, request_id).int)) if c.isdigit())
            ein = f"{digits[:2]}-{digits[2:9]}"
        first, _, last = (contact_name or business_name).strip().partition(" ")
        details: dict = {
            "business_details": {
                "business_name": business_name,
                "operating_country": country_code,
                "business_identifiers": [{"country_code": country_code, "number": ein, "type": "EIN"}],
            },
            "business_person_details": [{"first_name": first or business_name,
                                         "last_name": last or first or business_name,
                                         "roles": ["AUTHORISED_PERSON"]}],
        }
        create_payload: dict = {"account_details": details}
        if email:
            create_payload["primary_contact"] = {"email": email}
        created = self._request("POST", "/accounts/create", json=create_payload)
        account_id = created.get("id")
        if not account_id:
            raise AirwallexError("POST /accounts/create returned no account id.")
        # Fill gaps: the update repeats the identifiers (full replacement lists).
        self._request("POST", f"/accounts/{account_id}/update", json=create_payload)
        self._request("POST", f"/accounts/{account_id}/submit", json={})
        if confirm_wait_seconds:
            self._sleep(confirm_wait_seconds)  # the guide: wait ~2s after submit
        self._request("POST", f"/simulation/accounts/{account_id}/update_status",
                      json={"next_status": "ACTIVE", "force": True})
        confirmed = self._get(f"/accounts/{account_id}")
        status = confirmed.get("status")
        if status != "ACTIVE":
            raise AirwallexError(
                f"Connected account {account_id} is {status}, not ACTIVE after activation.")
        return {**confirmed, "id": account_id, "status": "ACTIVE", "business_name": business_name}

    def get_connected_account(self, *, account_id: str) -> dict:
        return self._get(f"/accounts/{account_id}")

    def create_hosted_onboarding_link(self, *, account_id: str, template: str, return_url: str = "") -> dict:
        """Start hosted onboarding: POST /hosted_flows/create.

        Returns the flow instance (``id``, ``url``, ``status``). The URL is
        not usable until it is authorised with
        :meth:`authorize_hosted_onboarding_link`.
        """
        payload: dict = {"account_id": account_id, "template": template}
        if return_url:
            payload["return_url"] = return_url
        return self._request("POST", "/hosted_flows/create", json=payload)

    def authorize_hosted_onboarding_link(self, *, flow_id: str) -> dict:
        return self._request("POST", f"/hosted_flows/{flow_id}/authorize", json={})

    def activate_connected_account(self, *, account_id: str) -> dict:
        """Sandbox only: force a connected account to ACTIVE.

        POST /simulation/accounts/{id}/update_status with
        ``next_status=ACTIVE`` and ``force=true`` (the account would
        otherwise need to be SUBMITTED first). Must complete before any
        FX or transfer for this employer.
        """
        return self._request(
            "POST", f"/simulation/accounts/{account_id}/update_status",
            on_behalf_of=account_id,
            json={"next_status": "ACTIVE", "force": True},
        )

    def get_balances(self, *, account_id: str | None = None) -> dict[str, Decimal]:
        data = self._request("GET", "/balances/current", on_behalf_of=account_id)
        out: dict[str, Decimal] = {}
        lines = data if isinstance(data, list) else data.get("items", [])
        for line in lines:
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
            request_id=request_id,
            duplicate_item_path="/deposits/{id}",
            duplicate_collection="/deposits",
        )

    # -- FX ---------------------------------------------------------------------
    def create_fx_quote(self, *, request_id: str, on_behalf_of: str, sell_currency: str,
                        buy_currency: str, buy_amount: object) -> dict:
        data = self._request(
            "POST", "/fx/quotes/create", on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "sell_currency": sell_currency,
                  "buy_currency": buy_currency,
                  "buy_amount": str(round_money(to_decimal(buy_amount)))},
            request_id=request_id,
            duplicate_item_path="/fx/quotes/{id}",
            duplicate_collection="/fx/quotes",
        )
        if "id" not in data and data.get("quote_id"):
            data["id"] = data["quote_id"]
        return data

    def get_fx_conversion(self, *, conversion_id: str, on_behalf_of: str) -> dict:
        return self._get(f"/fx/conversions/{conversion_id}", on_behalf_of=on_behalf_of)

    def create_fx_conversion(self, *, request_id: str, on_behalf_of: str, quote_id: str,
                             buy_amount: object, sell_currency: str = "USD", buy_currency: str = "EUR") -> dict:
        data = self._request(
            "POST", "/fx/conversions/create", on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "quote_id": quote_id,
                  "sell_currency": sell_currency, "buy_currency": buy_currency,
                  "buy_amount": str(round_money(to_decimal(buy_amount)))},
            request_id=request_id,
            duplicate_item_path="/fx/conversions/{id}",
            duplicate_collection="/fx/conversions",
        )
        if "id" not in data and data.get("conversion_id"):
            data["id"] = data["conversion_id"]
        return data

    # -- beneficiaries & transfers ----------------------------------------------
    def get_beneficiary(self, *, beneficiary_id: str, on_behalf_of: str) -> dict:
        return self._get(f"/beneficiaries/{beneficiary_id}", on_behalf_of=on_behalf_of)

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
        return self._request("POST", "/beneficiaries/create", on_behalf_of=on_behalf_of, json=payload,
                             request_id=request_id,
                             duplicate_item_path="/beneficiaries/{id}",
                             duplicate_collection="/beneficiaries")

    def get_transfer(self, *, transfer_id: str, on_behalf_of: str) -> dict:
        return self._get(f"/transfers/{transfer_id}", on_behalf_of=on_behalf_of)

    def create_transfer(self, *, request_id: str, on_behalf_of: str, beneficiary_id: str,
                        amount: object, currency: str = "EUR", method: str = "SWIFT",
                        reference: str = "") -> dict:
        payload: dict = {"request_id": request_id, "beneficiary_id": beneficiary_id,
                         "transfer_amount": str(round_money(to_decimal(amount))),
                         "transfer_currency": currency, "transfer_method": method,
                         "source_currency": currency, "fee_paid_by": "PAYER",
                         "reason": "professional_services"}
        if reference:
            payload["reference"] = reference
        return self._request(
            "POST", "/transfers/create", on_behalf_of=on_behalf_of, json=payload,
            request_id=request_id,
            duplicate_item_path="/transfers/{id}",
            duplicate_collection="/transfers",
        )

    def transition_transfer(self, *, request_id: str, on_behalf_of: str, transfer_id: str,
                            target_status: str = "PAID") -> dict:
        return self._request(
            "POST", f"/simulation/transfers/{transfer_id}/transition",
            on_behalf_of=on_behalf_of,
            json={"request_id": request_id, "target_status": target_status},
            request_id=request_id,
            duplicate_item_path="/transfers/{id}",
        )

    # -- platform charges ---------------------------------------------------------
    def get_charge(self, *, charge_id: str, on_behalf_of: str | None = None) -> dict:
        return self._get(f"/charges/{charge_id}", on_behalf_of=on_behalf_of)

    def create_charge(self, *, request_id: str, source_account_id: str, amount: object,
                      currency: str = "USD", reference: str = "") -> dict:
        """Collect the platform fee from ONE employer's connected account.

        Kit 6: ``POST /charges/create`` WITHOUT ``x-on-behalf-of`` (the
        platform is the beneficiary); the employer is named only in
        ``source``. See ``backend/docs/airwallex-fee-path.md``.
        """
        return self._request(
            "POST", "/charges/create",
            json={"request_id": request_id, "source": source_account_id,
                  "amount": str(round_money(to_decimal(amount))), "currency": currency,
                  "reason": "professional_business_services",
                  "reference": reference or "platform fee"},
            request_id=request_id,
            duplicate_item_path="/charges/{id}",
            duplicate_collection="/charges",
        )
