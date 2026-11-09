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
GLOBAL_ACCOUNT_ID = "6f1b7c0e-3a52-4d8e-9b41-2c7d5e8a9f10"
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

def test_create_connected_account_runs_kit6_activation_flow():
    client, calls = make_client({
        ("POST", "/accounts/create"): (201, load("connected_account_create.json")),
        ("POST", f"/accounts/{ACCOUNT_ID}/update"): (200, load("connected_account_create.json")),
        ("POST", f"/accounts/{ACCOUNT_ID}/submit"): (200, load("connected_account_create.json")),
        ("POST", f"/simulation/accounts/{ACCOUNT_ID}/update_status"): (200, load("connected_account_active.json")),
        ("GET", f"/accounts/{ACCOUNT_ID}"): (200, load("connected_account_active.json")),
    })
    account = client.create_connected_account(
        request_id="req-acct-1", business_name="Acme Manufacturing GmbH", contact_name="Ana Weber",
        email="ana.weber@acme.example", confirm_wait_seconds=0)
    assert account["id"] == ACCOUNT_ID and account["status"] == "ACTIVE"
    # Live API requires primary_contact (400 field_required without it, seen on the sandbox).
    assert only(calls, "/accounts/create")["json"]["primary_contact"] == {"email": "ana.weber@acme.example"}
    assert [(c["method"], c["path"].split("/api/v1")[-1]) for c in api_calls(calls)] == [
        ("POST", "/accounts/create"), ("POST", f"/accounts/{ACCOUNT_ID}/update"),
        ("POST", f"/accounts/{ACCOUNT_ID}/submit"),
        ("POST", f"/simulation/accounts/{ACCOUNT_ID}/update_status"), ("GET", f"/accounts/{ACCOUNT_ID}")]
    assert all("x-on-behalf-of" not in c["headers"] for c in api_calls(calls))
    upd = only(calls, f"/accounts/{ACCOUNT_ID}/update")["json"]
    ids = upd["account_details"]["business_details"]["business_identifiers"]
    assert ids[0]["type"] == "EIN" and ids[0]["number"]
    # Accounts API: operating_country is array[string] (a bare string -> 400 invalid_argument, seen live).
    for path in ("/accounts/create", f"/accounts/{ACCOUNT_ID}/update"):
        assert only(calls, path)["json"]["account_details"]["business_details"]["operating_country"] == ["US"]
    assert only(calls, f"/simulation/accounts/{ACCOUNT_ID}/update_status")["json"] == {
        "next_status": "ACTIVE", "force": True}


def test_create_connected_account_raises_if_not_active_after_activation():
    client, _ = make_client({
        ("POST", "/accounts/create"): (201, load("connected_account_create.json")),
        ("POST", f"/accounts/{ACCOUNT_ID}/update"): (200, {}),
        ("POST", f"/accounts/{ACCOUNT_ID}/submit"): (200, {}),
        ("POST", f"/simulation/accounts/{ACCOUNT_ID}/update_status"): (200, {}),
        ("GET", f"/accounts/{ACCOUNT_ID}"): (200, load("connected_account_create.json")),
    })
    with pytest.raises(AirwallexError, match="not ACTIVE"):
        client.create_connected_account(request_id="r", business_name="Acme", email="a@acme.example",
                                        confirm_wait_seconds=0)


def test_create_connected_account_requires_a_contact_email_before_calling_airwallex():
    client, calls = make_client({})
    with pytest.raises(AirwallexError, match="contact email"):
        client.create_connected_account(request_id="r", business_name="Acme", confirm_wait_seconds=0)
    assert api_calls(calls) == []  # failed fast: nothing was sent


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
    deposit = client.deposit(request_id="req-dep-1", account_id=ACCOUNT_ID, amount=Decimal("5000"),
                             global_account_id=GLOBAL_ACCOUNT_ID)
    assert deposit["status"] == "PENDING"  # live: PENDING, yet already in available_amount
    call = only(calls, "/simulation/deposit/create")
    assert call["json"]["global_account_id"] == GLOBAL_ACCOUNT_ID
    assert call["json"]["amount"] == 5000.0 and "currency" not in call["json"]
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_deposit_requires_a_global_account():
    client, calls = make_client({})
    with pytest.raises(AirwallexError, match="global_account_id"):
        client.deposit(request_id="r", account_id=ACCOUNT_ID, amount=Decimal("1"))
    assert api_calls(calls) == []


def test_deposit_is_never_retried_because_the_endpoint_ignores_request_id():
    client, calls = make_client({("POST", "/simulation/deposit/create"): (500, {"code": "operation_failed"})})
    with pytest.raises(AirwallexError):
        client.deposit(request_id="r", account_id=ACCOUNT_ID, amount=Decimal("1"),
                       global_account_id=GLOBAL_ACCOUNT_ID)
    assert len(api_calls(calls)) == 1


def test_create_global_account_shape():
    client, calls = make_client({("POST", "/global_accounts/create"): (201, load("global_account_create.json"))})
    ga = client.create_global_account(request_id="req-ga-1", account_id=ACCOUNT_ID)
    assert ga["id"] == GLOBAL_ACCOUNT_ID and ga["status"] == "ACTIVE"
    call = only(calls, "/global_accounts/create")
    assert call["json"]["required_features"] == [{"currency": "USD", "transfer_method": "LOCAL"}]
    assert call["json"]["country_code"] == "US"
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
    assert conversion["sell_amount"] == 6787.76
    quote_call = only(calls, "/fx/quotes/create")
    assert quote_call["json"]["buy_amount"] == "6025.70"
    assert quote_call["json"]["validity"] == "MIN_1"  # required by the live API
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
    assert transfer["fee_amount"] == 13.39  # live sandbox SWIFT fee (rate-dependent), not a flat 12.85
    call = only(calls, "/transfers/create")
    assert call["json"]["reference"]  # mandatory live
    assert call["json"]["source_currency"] == "EUR"
    assert call["json"]["fee_paid_by"] == "PAYER"
    assert call["json"]["transfer_amount"] == "3400.00"
    assert call["headers"]["x-on-behalf-of"] == ACCOUNT_ID


def test_transition_transfer_goes_processing_sent_paid():
    seq = {"n": 0}
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json=load("auth_login.json"))
        if request.method == "GET":
            return httpx.Response(200, json=load("transfer_get.json"))  # PROCESSING
        posts.append(json.loads(request.content))
        seq["n"] += 1
        return httpx.Response(200, json=load("transfer_sent.json" if seq["n"] == 1 else "transfer_paid.json"))

    client = AirwallexClient(Settings(app_env="live", airwallex_client_id="i", airwallex_api_key="k"),
                             transport=httpx.MockTransport(handler))
    result = client.transition_transfer(request_id="req-tt-1", on_behalf_of=ACCOUNT_ID,
                                        transfer_id=TRANSFER_ID, target_status="PAID")
    assert result["status"] == "PAID"
    # live: body field is next_status; PROCESSING -> PAID directly is a 500
    assert posts == [{"next_status": "SENT"}, {"next_status": "PAID"}]


def test_transition_transfer_already_paid_sends_nothing():
    client, calls = make_client({("GET", f"/transfers/{TRANSFER_ID}"): (200, load("transfer_paid.json"))})
    assert client.transition_transfer(request_id="r", on_behalf_of=ACCOUNT_ID, transfer_id=TRANSFER_ID,
                                      target_status="PAID")["status"] == "PAID"
    assert all(c["method"] == "GET" for c in api_calls(calls))


def test_charge_is_scoped_to_the_source_employer():
    client, calls = make_client({("POST", "/charges/create"): (201, load("charge_create.json"))})
    charge = client.create_charge(request_id="req-ch-1", source_account_id=ACCOUNT_ID,
                                  amount=Decimal("149"), reference="payroll run fee")
    assert charge["status"] in ("NEW", "SUCCEEDED", "SETTLED")
    call = only(calls, "/charges/create")
    assert call["json"]["source"] == ACCOUNT_ID and "source_id" not in call["json"]
    assert call["json"]["reason"] == "professional_business_services"
    assert call["json"]["amount"] == "149.00"
    assert "x-on-behalf-of" not in call["headers"]


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
    assert "x-on-behalf-of" not in get["headers"]  # platform-scoped charge


def test_conversion_duplicate_409_returns_original():
    client, _ = make_client({
        ("POST", "/fx/conversions/create"): (409, load("conversion_duplicate_409.json")),
        ("GET", f"/fx/conversions/{CONVERSION_ID}"): (200, load("fx_conversion_create.json")),
    })
    conversion = client.create_fx_conversion(request_id="fixture-conversion-request-id",
                                             on_behalf_of=ACCOUNT_ID, quote_id="quo_x",
                                             buy_amount=Decimal("6025.70"))
    assert conversion["id"] == CONVERSION_ID


def test_conversion_duplicate_400_invalid_argument_returns_original():
    # Live: a reused conversion request_id is 400 invalid_argument "...used in the past".
    client, calls = make_client({
        ("POST", "/fx/conversions/create"): (400, load("conversion_duplicate_400.json")),
        ("GET", "/fx/conversions"): (200, {"items": [load("fx_conversion_create.json") | {"id": CONVERSION_ID}]}),
    })
    conversion = client.create_fx_conversion(request_id="fixture-conversion-request-id",
                                             on_behalf_of=ACCOUNT_ID, quote_id="quo_x",
                                             buy_amount=Decimal("6025.70"))
    assert conversion["id"] == CONVERSION_ID
    assert len([c for c in api_calls(calls) if c["method"] == "POST"]) == 1


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
    assert rates["EUR"] == Decimal("1.1264")  # 112.64 USD sold / 100.00 EUR bought
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


# -- v3 fixes: backoff, duplicate recovery safety, bare arrays -------------------

def _scripted_client(responses, sleeps):
    """Client whose responses (after login) replay ``responses`` in order."""
    calls: list[httpx.Request] = []
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json=load("auth_login.json"))
        calls.append(request)
        status, body, headers = next(it)
        return httpx.Response(status, json=body, headers=headers)

    settings = Settings(app_env="live", airwallex_client_id="c", airwallex_api_key="k",
                        airwallex_fee_config_retry_seconds=60, airwallex_rate_limit_retries=3)
    client = AirwallexClient(settings, transport=httpx.MockTransport(handler))
    client._sleep = sleeps.append  # type: ignore[method-assign]
    return client, calls


def test_fx_has_no_api_version_header_and_retries_unconfigured_fee_with_same_request_id():
    sleeps: list[float] = []
    client, calls = _scripted_client([
        (400, {"code": "unconfigured_client_fee", "message": "x"}, {}),
        (400, {"code": "unconfigured_client_fee", "message": "x"}, {}),
        (201, load("fx_conversion_create.json"), {}),
    ], sleeps)
    client.create_fx_conversion(request_id="rid-1", on_behalf_of=ACCOUNT_ID, quote_id="q",
                                buy_amount=Decimal("10"))
    assert len(calls) == 3 and len(sleeps) == 2
    assert {json.loads(c.content)["request_id"] for c in calls} == {"rid-1"}
    assert all("x-api-version" not in c.headers for c in calls)


def test_unconfigured_fee_retry_is_bounded():
    sleeps: list[float] = []
    client, _ = _scripted_client([(400, {"code": "unconfigured_client_fee"}, {})] * 40, sleeps)
    with pytest.raises(AirwallexError) as exc:
        client.create_fx_conversion(request_id="rid", on_behalf_of=ACCOUNT_ID, quote_id="q",
                                    buy_amount=Decimal("10"))
    assert exc.value.code == "unconfigured_client_fee"
    assert sum(sleeps) <= 60


def test_429_honours_retry_after_then_succeeds_and_is_bounded():
    sleeps: list[float] = []
    client, calls = _scripted_client([
        (429, {"code": "too_many_requests"}, {"Retry-After": "2"}),
        (201, load("fx_conversion_create.json"), {}),
    ], sleeps)
    client.create_fx_conversion(request_id="rid", on_behalf_of=ACCOUNT_ID, quote_id="q",
                                buy_amount=Decimal("10"))
    assert sleeps == [2.0] and len(calls) == 2
    sleeps2: list[float] = []
    client2, calls2 = _scripted_client([(429, {"code": "too_many_requests"}, {})] * 10, sleeps2)
    with pytest.raises(AirwallexError) as exc:
        client2.create_fx_conversion(request_id="rid", on_behalf_of=ACCOUNT_ID, quote_id="q",
                                     buy_amount=Decimal("10"))
    assert exc.value.status_code == 429 and len(calls2) == 4  # 1 + 3 retries


def test_duplicate_recovery_rejects_list_item_with_other_request_id():
    wrong = dict(load("transfer_create.json"), request_id="someone-elses-request")
    client, _ = make_client({
        ("POST", "/transfers/create"): (409, {"code": "duplicate_request_id"}),
        ("GET", "/transfers"): (200, {"items": [wrong]}),
    })
    with pytest.raises(AirwallexError, match="Duplicate request_id"):
        client.create_transfer(request_id="mine", on_behalf_of=ACCOUNT_ID, beneficiary_id="b",
                               amount=Decimal("10"))


def test_duplicate_recovery_picks_the_matching_list_item():
    wrong = dict(load("transfer_create.json"), id="trf_wrong", request_id="other")
    right = dict(load("transfer_create.json"), id="trf_right", request_id="mine")
    client, _ = make_client({
        ("POST", "/transfers/create"): (409, {"code": "duplicate_request_id"}),
        ("GET", "/transfers"): (200, {"items": [wrong, right]}),
    })
    out = client.create_transfer(request_id="mine", on_behalf_of=ACCOUNT_ID, beneficiary_id="b",
                                 amount=Decimal("10"))
    assert out["id"] == "trf_right"


def test_get_balances_accepts_bare_array_and_items_wrapper():
    arr = load("balances_current.json")
    client, _ = make_client({("GET", "/balances/current"): (200, arr)})
    assert client.get_balances(account_id=ACCOUNT_ID)["USD"] == Decimal("13343.24")
    client2, _ = make_client({("GET", "/balances/current"): (200, {"items": arr})})
    assert client2.get_balances(account_id=ACCOUNT_ID)["USD"] == Decimal("13343.24")


def test_default_base_url_is_sandbox():
    assert Settings(_env_file=None).airwallex_base_url == "https://api.sandbox.airwallex.com/api/v1"
