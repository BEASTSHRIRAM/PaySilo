"""v3 bedata fix-ups: import commit goes through the graph, atomic commit,
re-import duplicates, receipt isolation/expiry, export routes, real
reconciliation variance, hardening."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.test_bedata import ACME, CSV_OK, _employers, _pay_line_id, _settle_acme


def _preview(c, emp_id, text, name="f.csv", mapping=None):
    body = {"filename": name, "content": text}
    if mapping:
        body["mapping"] = mapping
    r = c.post(f"/employers/{emp_id}/imports/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- import commit

def test_import_commit_runs_through_graph_to_approval_and_settles(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    imp = _preview(demo_client, acme_id, CSV_OK)
    assert imp["needs_contractor_count"] == 0
    r = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["source"] == "import"
    assert run["status"] != "RECEIVED", run["status"]  # priced by the graph, not stranded
    assert run["pending"] is not None  # awaiting a TAP-backed human decision
    assert all(p["payout_fee"] is not None for p in run["pay_lines"])
    done = demo_client.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED", done
    assert all(p["status"] == "PAID" for p in done["pay_lines"])


def test_import_commit_unmatched_rows_need_a_contractor(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    text = "name,amount,currency,iban\nNew Person,100,EUR,DE02120300000000202051\n"
    imp = _preview(demo_client, acme_id, text)
    assert imp["needs_contractor_count"] == 1 and imp["rows"][0]["needs_contractor"] is True
    r = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={})
    assert r.status_code == 422
    assert r.json()["detail"]["needs_contractor"][0]["beneficiary_name"] == "New Person"
    # No beneficiary was silently created and the import stays committable.
    assert len(demo_client.get(f"/employers/{acme_id}").json()["contractors"]) == 2
    assert demo_client.get(f"/employers/{acme_id}/imports/{imp['import_id']}").json()["status"] == "PREVIEWED"


def test_import_commit_zero_valid_rows_422(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    imp = _preview(demo_client, acme_id, "name,amount,currency,iban\nBroken,-1,EUR,DE89370400440532013000\n")
    assert demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={}).status_code == 422


def test_import_commit_inactive_employer_409(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    imp = _preview(demo_client, acme_id, CSV_OK)
    with demo_client.app.state.db.sessionmaker() as s:
        from app.db.models import Employer
        s.get(Employer, uuid.UUID(acme_id)).status = "SUSPENDED"
        s.commit()
    assert demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={}).status_code == 409


def test_import_commit_race_loser_gets_409_and_no_second_run(demo_client, monkeypatch):
    from app.api.routes import bedata

    acme_id = _employers(demo_client)[ACME]["id"]
    imp = _preview(demo_client, acme_id, CSV_OK)
    real = bedata.match_contractor
    fired = {"n": 0}

    def racing(contractors, data):
        if not fired["n"]:  # a concurrent request claims the import after we loaded PREVIEWED
            fired["n"] = 1
            with demo_client.app.state.db.sessionmaker() as s:
                from app.db.models import SourceImport
                s.get(SourceImport, uuid.UUID(imp["import_id"])).status = "COMMITTED"
                s.commit()
        return real(contractors, data)

    monkeypatch.setattr(bedata, "match_contractor", racing)
    r = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={})
    assert r.status_code == 409, r.text
    runs = demo_client.get("/runs", params={"employer_id": acme_id}).json()
    assert [x for x in runs if x["source"] == "import"] == []


def test_reimport_in_different_order_is_flagged_duplicate(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    first = _preview(demo_client, acme_id, CSV_OK)
    assert demo_client.post(f"/employers/{acme_id}/imports/{first['import_id']}/commit", json={}).status_code == 200
    reordered = (
        "name,email,amount,currency,method,iban,account_number,swift_code,country\n"
        "Tomas Rivera,,900,EUR,SWIFT,ES9121000418450200051332,,CAIXESBBXXX,ES\n"
        "Lena Fischer,lena@example.com,1200,EUR,SWIFT,DE89370400440532013000,,COBADEFFXXX,DE\n"
    )
    again = _preview(demo_client, acme_id, reordered, name="reordered.csv")
    assert [r["status"] for r in again["rows"]] == ["duplicate", "duplicate"], again
    assert again["duplicate_count"] == 2


def test_preview_with_new_mapping_reruns(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    text = "worker,pay_amt,ccy,bank\nLena Fischer,42,EUR,DE89370400440532013000\n"
    bad = _preview(demo_client, acme_id, text)
    assert bad["valid_count"] == 0
    good = _preview(demo_client, acme_id, text, mapping={
        "worker": "beneficiary_name", "pay_amt": "amount", "ccy": "currency", "bank": "iban"})
    assert good["import_id"] == bad["import_id"] and good["valid_count"] == 1


def test_amount_strict_and_row_cap(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    text = ("name,amount,currency,iban\n"
            "A,1_000,EUR,DE89370400440532013000\n"
            "B,1e3,EUR,DE89370400440532013000\n"
            "C,1.234,EUR,DE89370400440532013000\n"
            "D,1,200.50,EUR,DE89370400440532013000\n")
    body = _preview(demo_client, acme_id, text)
    assert [r["status"] for r in body["rows"][:3]] == ["error", "error", "error"]
    big = "name,amount,currency,iban\n" + "X,1,EUR,DE89370400440532013000\n" * 5001
    r = demo_client.post(f"/employers/{acme_id}/imports/preview", json={"filename": "big.csv", "content": big})
    assert r.status_code == 413


# ----------------------------------------------------------------- receipts

def test_receipt_exposes_only_own_line_and_expires(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    tok = demo_client.get(f"/runs/{done['id']}/pay-lines/{line_id}/receipt-token").json()
    assert tok["path"].startswith("/receipt/")
    demo_client.post("/auth/logout")
    r = demo_client.get(f"/receipts/{line_id}", params={"token": tok["token"]})
    assert r.status_code == 200
    text = r.text
    assert "Lena Fischer" in text
    for leak in ("Tomas", "Rivera", "2600", "acct_", "ES9121", "6000"):
        assert leak not in text, leak
    assert any("settled" in e["label"].lower() for e in r.json()["timeline"])
    # Tampered expiry / expired token -> 404.
    exp, sig = tok["token"].split(".")
    assert demo_client.get(f"/receipts/{line_id}", params={"token": f"{int(exp) + 99}.{sig}"}).status_code == 404

    from app.api.routes.bedata import receipt_token
    settings = demo_client.app.state.settings
    old = receipt_token(settings, uuid.UUID(line_id), now=datetime.now(UTC) - timedelta(days=60))
    assert demo_client.get(f"/receipts/{line_id}", params={"token": old}).status_code == 404


# ----------------------------------------------------------- exports / links

def test_platform_journal_export_and_employer_export(demo_client):
    _settle_acme(demo_client)
    r = demo_client.get("/platform/journals/export", params={"format": "csv"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "platform:fees:" in r.text
    r = demo_client.get("/platform/journals/export", params={"format": "json"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]


# ------------------------------------------------------------ reconciliation

def test_reconciliation_detects_wallet_variance_and_empty_percent(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    rec = demo_client.get(f"/employers/{acme_id}/reconciliation").json()
    assert rec["basis"] == "ledger_vs_wallet"
    assert all(r["wallet_available"] and r["variance"] == "0.00" for r in rec["currencies"])
    # Provider wallet drifts from the ledger by USD 25.00.
    runtime = demo_client.app.state.runtime
    with demo_client.app.state.db.sessionmaker() as s:
        from app.db.models import Employer
        acct = s.get(Employer, uuid.UUID(acme_id)).airwallex_account_id
    runtime.recorder.inner._credit(acct, "USD", Decimal("25.00"))
    rec = demo_client.get(f"/employers/{acme_id}/reconciliation").json()
    usd = {r["currency"]: r for r in rec["currencies"]}["USD"]
    assert usd["variance"] == "-25.00" and usd["status"] == "variance" and rec["status"] == "variance"
    # No pay lines at all -> not "100% matched".
    cobalt = _employers(demo_client)["Cobalt Retail LLC"]["id"]
    assert demo_client.get(f"/employers/{cobalt}/reconciliation").json()["matched_percent"] is None


# ---------------------------------------------------------------- exceptions

def test_exceptions_limit_and_offset(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    for i in range(3):
        _preview(demo_client, acme_id, f"name,amount,currency,iban\nBroken{i},-1,EUR,DE89370400440532013000\n",
                 name=f"b{i}.csv")
    assert len(demo_client.get("/exceptions").json()) >= 3
    assert len(demo_client.get("/exceptions", params={"limit": 2}).json()) == 2
    assert len(demo_client.get("/exceptions", params={"limit": 500, "offset": 1000}).json()) == 0
