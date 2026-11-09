"""Live Airwallex client tests — no keys, no network.

Every test replays recorded, sandbox-shaped responses (JSON fixtures under
``tests/fixtures/airwallex/``) through ``httpx.MockTransport`` and asserts
the exact request shape the client emits: method, path, payload, and the
``x-on-behalf-of`` header on every employer-scoped call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.airwallex.client import AirwallexClient, AirwallexError
from app.airwallex.rates import LiveFxRateProvider, LiveRateUnavailable
from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "airwallex"

ACCOUNT_ID = "acct_hkdmCy9wnfEJjMHNxINdC"
TRANSFER_ID = "trf_8e5b1a4d6c9f4e2b7d30"
CHARGE_ID = "chg_2d6a9c4f7b1e4a8d0c36"
CONVERSION_ID = "cnv_9d2b6f4a1c8e4b7d0a53"
DEPOSIT_ID = "dep_7f3a2c1e9b4d4f0aa1c2"
BENEFICIARY_ID = "bene_4a7d0c3f9e2b4d6a8c15"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_client(routes: dict[tuple[str, str], tuple[int, dict]]):
    """Build a client whose transport serves ``routes`` keyed by (method, path suffix)."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        calls.append({
            "method": request.method,
            "path": request.url.path,
            "headers": request.headers,
            "json": json.loads(request.content) if request.content else None,
            "params": dict(request.url.params),
        })
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json=load("auth_login.json"))
        for (method, suffix), (status, payload) in routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"code": "not_found", "message": request.url.path})

    settings = Settings(app_env="live", airwallex_client_id="fixture-client-id",
                        airwallex_api_key="fixture-api-key")
    client = AirwallexClient(settings, transport=httpx.MockTransport(handler))
    return client, calls


def api_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if not c["path"].endswith("/authentication/login")]


def only(calls: list[dict], suffix: str) -> dict:
    matches = [c for c in api_calls(calls) if c["path"].endswith(suffix)]
    assert len(matches) == 1, [c["path"] for c in api_calls(calls)]
    return matches[0]


# -- auth & balances -----------------------------------------------------------

def test_auth_uses_client_id_and_api_key_headers():
    client, calls = make_client({("GET", "/balances/current"): (200, load("balances_current.json"))})
    client.get_balances(account_id=ACCOUNT_ID)
    login = [c for c in calls if c["path"].endswith("/authentication/login")]
    assert len(login) == 1
    assert login[0]["method"] == "POST"
    assert login[0]["headers"]["x-client-id"] == "fixture-client-id"
    assert login[0]["headers"]["x-api-key"] == "fixture-api-key"


def test_balances_parse_decimals_and_carry_on_behalf_of():
    client, calls = make_client({("GET", "/balances/current"): (200, load("balances_current.json"))})
    balances = client.get_balances(account_id=ACCOUNT_ID)
    assert balances["USD"] == Decimal("13343.24")
    call = only(calls, "/balances/current")
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID
    assert call["headers"]["authorization"].startswith("Bearer ")


def test_platform_balances_have_no_on_behalf_of():
    client, calls = make_client({("GET", "/balances/current"): (200, load("balances_current.json"))})
    client.get_balances()
    assert "x-on-behalf-of" not in only(calls, "/balances/current")["headers"]


# -- connected accounts & onboarding --------------------------------------------

def test_create_connected_account_is_platform_scoped():
    client, calls = make_client(
        {("POST", "/connected_accounts/create"): (201, load("connected_account_create.json"))})
    account = client.create_connected_account(
        request_id="req-acct-1", business_name="Acme Manufacturing GmbH", contact_name="Ana Weber")
    assert account["status"] == "CREATED"
    call = only(calls, "/connected_accounts/create")
    assert call["json"] == {"request_id": "req-acct-1", "business_name": "Acme Manufacturing GmbH",
                            "contact_name": "Ana Weber"}
    assert "x-on-behalf-of" not in call["headers"]


def test_hosted_onboarding_link_create_and_authorize():
    client, calls = make_client({
        ("POST", "/hosted_flows/create"): (201, load("hosted_flow_create.json")),
        ("POST", "/hosted_flows/ahfi_IriUYRv-TuWIHnst2Dj4pw/authorize"): (200, load("hosted_flow_authorize.json")),
    })
    flow = client.create_hosted_onboarding_link(
        account_id=ACCOUNT_ID, template="tmpl_fixture", return_url="https://vertex.example/return")
    assert flow["url"].startswith("https://www.sandbox.airwallex.com/")
    create = only(calls, "/hosted_flows/create")
    assert create["json"] == {"account_id": ACCOUNT_ID, "template": "tmpl_fixture",
                              "return_url": "https://vertex.example/return"}
    authorised = client.authorize_hosted_onboarding_link(flow_id=flow["id"])
    assert authorised["authorization_code"] == "fixture-authorization-code"


def test_activate_connected_account_uses_simulation_update_status():
    client, calls = make_client(
        {("POST", f"/simulation/accounts/{ACCOUNT_ID}/update_status"): (200, load("connected_account_active.json"))})
    account = client.activate_connected_account(account_id=ACCOUNT_ID)
    assert account["status"] == "ACTIVE"
    call = only(calls, f"/simulation/accounts/{ACCOUNT_ID}/update_status")
    assert call["json"] == {"next_status": "ACTIVE", "force": True}
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_activation_happens_before_any_fx_quote():
    client, calls = make_client({
        ("POST", f"/simulation/accounts/{ACCOUNT_ID}/update_status"): (200, load("connected_account_active.json")),
        ("POST", "/fx/quotes/create"): (201, load("fx_quote_create.json")),
    })
    client.activate_connected_account(account_id=ACCOUNT_ID)
    provider = LiveFxRateProvider(client)
    provider.rates_for({"EUR"}, account_id=ACCOUNT_ID, run_id="run-order")
    paths = [c["path"] for c in api_calls(calls)]
    assert paths.index(next(p for p in paths if "update_status" in p)) < \
        paths.index(next(p for p in paths if p.endswith("/fx/quotes/create")))


# -- funding, FX, beneficiaries, transfers, charges -------------------------------

def test_deposit_shape():
    client, calls = make_client(
        {("POST", "/simulation/deposit/create"): (201, load("simulation_deposit_create.json"))})
    deposit = client.deposit(request_id="req-dep-1", account_id=ACCOUNT_ID, amount=Decimal("20000"))
    assert deposit["status"] == "SETTLED"
    call = only(calls, "/simulation/deposit/create")
    assert call["json"] == {"request_id": "req-dep-1", "amount": "20000.00", "currency": "USD"}
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_fx_quote_and_conversion_shapes_and_id_normalisation():
    client, calls = make_client({
        ("POST", "/fx/quotes/create"): (201, load("fx_quote_create.json")),
        ("POST", "/fx/conversions/create"): (201, load("fx_conversion_create.json")),
    })
    quote = client.create_fx_quote(request_id="req-q-1", on_behalf_of=ACCOUNT_ID,
                                   sell_currency="USD", buy_currency="EUR",
                                   buy_amount=Decimal("6025.70"))
    conversion = client.create_fx_conversion(request_id="req-c-1", on_behalf_of=ACCOUNT_ID,
                                             quote_id=quote["id"], buy_amount=Decimal("6025.70"))
    # The conversion fixture identifies itself by conversion_id; the client
    # normalises it to id so graph code can treat both backends alike.
    assert conversion["id"] == CONVERSION_ID
    assert conversion["sell_amount"] == "6507.76"
    quote_call = only(calls, "/fx/quotes/create")
    assert quote_call["json"]["buy_amount"] == "6025.70"
    assert quote_call["headers"]["x-on-behalf-of"] == ACCOUNT_ID
    conv_call = only(calls, "/fx/conversions/create")
    assert conv_call["json"]["quote_id"] == quote["id"]


def test_beneficiary_validates_before_create():
    client, calls = make_client({
        ("POST", "/beneficiaries/validate"): (200, load("beneficiary_validate.json")),
        ("POST", "/beneficiaries/create"): (201, load("beneficiary_create.json")),
    })
    bene = client.create_beneficiary(request_id="req-b-1", on_behalf_of=ACCOUNT_ID,
                                     name="Lena Fischer", iban="DE89370400440532013000",
                                     swift_code="COBADEFFXXX")
    assert bene["id"] == BENEFICIARY_ID
    paths = [c["path"] for c in api_calls(calls)]
    assert paths.index(next(p for p in paths if p.endswith("/beneficiaries/validate"))) < \
        paths.index(next(p for p in paths if p.endswith("/beneficiaries/create")))
    create = only(calls, "/beneficiaries/create")
    assert create["json"]["beneficiary"]["bank_details"]["iban"] == "DE89370400440532013000"
    assert create["json"]["transfer_methods"] == ["SWIFT"]
    assert create["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_transfer_shape_payer_pays_and_wallet_holds_transfer_currency():
    client, calls = make_client({("POST", "/transfers/create"): (201, load("transfer_create.json"))})
    transfer = client.create_transfer(request_id="req-t-1", on_behalf_of=ACCOUNT_ID,
                                      beneficiary_id=BENEFICIARY_ID, amount=Decimal("3400"))
    assert transfer["fee_amount"] == 12.85
    call = only(calls, "/transfers/create")
    assert call["json"]["source_currency"] == "EUR"
    assert call["json"]["fee_paid_by"] == "PAYER"
    assert call["json"]["transfer_amount"] == "3400.00"
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_transition_transfer_shape():
    client, calls = make_client(
        {("POST", f"/simulation/transfers/{TRANSFER_ID}/transition"): (200, load("transfer_get.json"))})
    result = client.transition_transfer(request_id="req-tt-1", on_behalf_of=ACCOUNT_ID,
                                        transfer_id=TRANSFER_ID, target_status="PAID")
    assert result["id"] == TRANSFER_ID
    call = only(calls, f"/simulation/transfers/{TRANSFER_ID}/transition")
    assert call["json"] == {"request_id": "req-tt-1", "target_status": "PAID"}


def test_charge_is_scoped_to_the_source_employer():
    client, calls = make_client({("POST", "/charges/create"): (201, load("charge_create.json"))})
    charge = client.create_charge(request_id="req-ch-1", source_account_id=ACCOUNT_ID,
                                  amount=Decimal("149"), reference="payroll run fee")
    assert charge["status"] == "SUCCEEDED"
    call = only(calls, "/charges/create")
    assert call["json"]["source_id"] == ACCOUNT_ID
    assert call["json"]["amount"] == "149.00"
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


# -- duplicate request_id: fetch the original instead of failing ------------------

def test_transfer_duplicate_409_returns_original():
    client, calls = make_client({
        ("POST", "/transfers/create"): (409, load("transfer_duplicate_409.json")),
        ("GET", f"/transfers/{TRANSFER_ID}"): (200, load("transfer_get.json")),
    })
    transfer = client.create_transfer(request_id="fixture-transfer-request-id",
                                      on_behalf_of=ACCOUNT_ID, beneficiary_id=BENEFICIARY_ID,
                                      amount=Decimal("3400"))
    assert transfer["id"] == TRANSFER_ID
    assert transfer["status"] == "PROCESSING"  # the original, fetched — not a new transfer
    posts = [c for c in api_calls(calls) if c["method"] == "POST" and c["path"].endswith("/transfers/create")]
    assert len(posts) == 1  # never retried with a new id


def test_charge_duplicate_error_code_returns_original():
    # Charges report a reused request_id as a 400 with code request_id_duplicate.
    client, calls = make_client({
        ("POST", "/charges/create"): (400, load("charge_duplicate_400.json")),
        ("GET", f"/charges/{CHARGE_ID}"): (200, load("charge_create.json")),
    })
    charge = client.create_charge(request_id="fixture-charge-request-id",
                                  source_account_id=ACCOUNT_ID, amount=Decimal("149"))
    assert charge["id"] == CHARGE_ID
    get = only(calls, f"/charges/{CHARGE_ID}")
    assert get["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_conversion_duplicate_409_returns_original():
    client, _ = make_client({
        ("POST", "/fx/conversions/create"): (409, load("conversion_duplicate_409.json")),
        ("GET", f"/fx/conversions/{CONVERSION_ID}"): (200, load("fx_conversion_create.json")),
    })
    conversion = client.create_fx_conversion(request_id="fixture-conversion-request-id",
                                             on_behalf_of=ACCOUNT_ID, quote_id="quo_x",
                                             buy_amount=Decimal("6025.70"))
    assert conversion["id"] == CONVERSION_ID


def test_deposit_duplicate_409_returns_original():
    client, _ = make_client({
        ("POST", "/simulation/deposit/create"): (409, load("deposit_duplicate_409.json")),
        ("GET", f"/deposits/{DEPOSIT_ID}"): (200, load("simulation_deposit_create.json")),
    })
    deposit = client.deposit(request_id="fixture-deposit-request-id", account_id=ACCOUNT_ID,
                             amount=Decimal("20000"))
    assert deposit["id"] == DEPOSIT_ID


def test_beneficiary_duplicate_409_returns_original():
    client, _ = make_client({
        ("POST", "/beneficiaries/validate"): (200, load("beneficiary_validate.json")),
        ("POST", "/beneficiaries/create"): (409, load("beneficiary_duplicate_409.json")),
        ("GET", f"/beneficiaries/{BENEFICIARY_ID}"): (200, load("beneficiary_create.json")),
    })
    bene = client.create_beneficiary(request_id="fixture-beneficiary-request-id",
                                     on_behalf_of=ACCOUNT_ID, name="Lena Fischer",
                                     iban="DE89370400440532013000")
    assert bene["id"] == BENEFICIARY_ID


def test_non_duplicate_error_preserves_provider_code_and_message():
    client, _ = make_client({
        ("POST", "/transfers/create"): (400, {"code": "insufficient_funds",
                                              "message": "The wallet balance is too low."}),
    })
    with pytest.raises(AirwallexError) as excinfo:
        client.create_transfer(request_id="req-t-9", on_behalf_of=ACCOUNT_ID,
                               beneficiary_id=BENEFICIARY_ID, amount=Decimal("3400"))
    assert excinfo.value.code == "insufficient_funds"
    assert "too low" in str(excinfo.value)


# -- live FX rates ------------------------------------------------------------------

def test_live_rate_provider_derives_usd_per_unit_from_quote_amounts():
    client, calls = make_client({("POST", "/fx/quotes/create"): (201, load("fx_quote_create.json"))})
    provider = LiveFxRateProvider(client)
    rates = provider.rates_for({"EUR", "USD"}, account_id=ACCOUNT_ID, run_id="run-rates")
    assert rates["USD"] == Decimal("1")
    assert rates["EUR"] == Decimal("1.08")  # 108.00 USD sold / 100.00 EUR bought
    quote_call = only(calls, "/fx/quotes/create")
    assert quote_call["headers"]["x-on-behalf-of"] == ACCOUNT_ID
    assert quote_call["json"]["sell_currency"] == "USD"
    assert quote_call["json"]["buy_currency"] == "EUR"


def test_live_rate_provider_refuses_when_quote_has_no_rate():
    client, _ = make_client({("POST", "/fx/quotes/create"): (201, {"id": "quo_x", "status": "QUOTED"})})
    provider = LiveFxRateProvider(client)
    with pytest.raises(LiveRateUnavailable, match="no usable rate"):
        provider.rates_for({"EUR"}, account_id=ACCOUNT_ID, run_id="run-norate")


def test_live_rate_provider_refuses_when_quote_request_fails():
    client, _ = make_client({("POST", "/fx/quotes/create"): (500, {"code": "server_error",
                                                                   "message": "boom"})})
    provider = LiveFxRateProvider(client)
    with pytest.raises(LiveRateUnavailable, match="unavailable"):
        provider.rates_for({"EUR"}, account_id=ACCOUNT_ID, run_id="run-500")


def test_live_rate_provider_requires_an_account():
    provider = LiveFxRateProvider(object())
    with pytest.raises(LiveRateUnavailable, match="connected account"):
        provider.rates_for({"EUR"}, account_id=None, run_id="run-noacct")
