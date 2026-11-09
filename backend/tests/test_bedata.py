"""v3 bedata lane: imports, ledger entries, funding, reconciliation,
journals, evidence, exceptions, calendar, worker receipts.

Golden numbers are the demo seed's: Acme settles EUR 3,400 + EUR 2,600
with 2 x EUR 12.85 SWIFT fees and the USD 149.00 platform fee
(final USD balance 13,343.24); Borealis is held short by USD 2,642.88.
"""

from __future__ import annotations

import hashlib
import json

ACME = "Acme Manufacturing GmbH"
BOREALIS = "Borealis Labs AB"
COBALT = "Cobalt Retail LLC"

CSV_OK = (
    "name,email,amount,currency,method,iban,account_number,swift_code,country\n"
    "Nina New,nina@example.com,1200,EUR,SWIFT,DE89370400440532013000,,COBADEFFXXX,DE\n"
    "Omar New,,900,USD,LOCAL,,123456789,,US\n"
)


def _employers(c):
    return {e["name"]: e for e in c.get("/employers").json()}


def _detail(c, name):
    return c.get(f"/employers/{_employers(c)[name]['id']}").json()


def _settle_acme(c):
    acme = _detail(c, ACME)
    run = c.post("/runs", json={"employer_id": acme["id"],
                                "lines": [{"contractor_id": x["id"]} for x in acme["contractors"]]}).json()
    done = c.post(f"/runs/{run['id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "SETTLED", done
    return done


def _hold_borealis(c):
    borealis = _detail(c, BOREALIS)
    run = c.post("/runs", json={"employer_id": borealis["id"],
                                "lines": [{"contractor_id": borealis["contractors"][0]["id"]}]}).json()
    assert run["status"] == "HELD_SHORTFALL", run
    return run


# ---------------------------------------------------------------- imports

def test_import_preview_validates_and_persists(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    r = demo_client.post(f"/employers/{acme_id}/imports/preview",
                         json={"filename": "nov.csv", "content": CSV_OK})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content_hash"] == hashlib.sha256(CSV_OK.encode()).hexdigest()
    assert body["row_count"] == 2 and body["valid_count"] == 2
    assert body["error_count"] == 0 and body["duplicate_count"] == 0
    assert body["rows"][0]["data"]["beneficiary_name"] == "Nina New"
    assert body["rows"][0]["data"]["amount"] == "1200.00"
    assert body["summary"]["valid_count"] == 2


def test_import_preview_same_file_twice_returns_same_import(demo_client):
    """T25: identical file imported twice -> no duplicate obligations."""
    acme_id = _employers(demo_client)[ACME]["id"]
    first = demo_client.post(f"/employers/{acme_id}/imports/preview",
                             json={"filename": "nov.csv", "content": CSV_OK}).json()
    second = demo_client.post(f"/employers/{acme_id}/imports/preview",
                              json={"filename": "nov.csv", "content": CSV_OK}).json()
    assert second["import_id"] == first["import_id"]
    # Commit once, re-preview is still the same (now committed) import.
    committed = demo_client.post(f"/employers/{acme_id}/imports/{first['import_id']}/commit", json={})
    assert committed.status_code == 200
    third = demo_client.post(f"/employers/{acme_id}/imports/preview",
                             json={"filename": "nov.csv", "content": CSV_OK}).json()
    assert third["import_id"] == first["import_id"] and third["status"] == "COMMITTED"


def test_import_preview_flags_errors_and_duplicates(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    bad = (
        "name,amount,currency,iban\n"
        ",500,EUR,DE89370400440532013000\n"                       # no name -> error
        "Bad Amount,not-a-number,EUR,DE89370400440532013000\n"   # bad amount -> error
        "Bad Ccy,100,JPY,DE89370400440532013000\n"               # unsupported ccy -> error
        "No Bank,100,EUR,\n"                                      # no bank -> error
        "Dup One,100,EUR,DE89370400440532013000\n"               # ok
        "Dup One,100,EUR,DE89370400440532013000\n"               # duplicate within file
    )
    body = demo_client.post(f"/employers/{acme_id}/imports/preview",
                            json={"filename": "bad.csv", "content": bad}).json()
    statuses = [r["status"] for r in body["rows"]]
    assert statuses == ["error", "error", "error", "error", "ok", "duplicate"], body
    assert body["error_count"] == 4 and body["duplicate_count"] == 1 and body["valid_count"] == 1


def test_import_duplicate_against_existing_run_pay_lines(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    # Lena Fischer EUR 3400 on the same IBAN as her contractor record.
    csv_text = ("name,amount,currency,iban\n"
                "Lena Fischer,3400,EUR,DE89370400440532013000\n"
                "Fresh Person,10,EUR,DE89370400440532013001\n")
    body = demo_client.post(f"/employers/{acme_id}/imports/preview",
                            json={"filename": "again.csv", "content": csv_text}).json()
    assert body["rows"][0]["status"] == "duplicate", body
    assert body["rows"][1]["status"] == "ok"


def test_import_commit_creates_one_run_and_is_idempotent(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    imp = demo_client.post(f"/employers/{acme_id}/imports/preview",
                           json={"filename": "nov.csv", "content": CSV_OK}).json()
    r1 = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit",
                          json={"period_label": "Nov 2026", "pay_date": "2026-11-30"})
    assert r1.status_code == 200, r1.text
    run = r1.json()
    assert run["source"] == "import" and run["period_label"] == "Nov 2026"
    assert len(run["pay_lines"]) == 2
    assert {p["beneficiary_name"] for p in run["pay_lines"]} == {"Nina New", "Omar New"}
    # Second commit: same run, nothing new.
    r2 = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={})
    assert r2.json()["id"] == run["id"]
    runs = demo_client.get("/runs", params={"employer_id": acme_id}).json()
    assert len([x for x in runs if x["source"] == "import"]) == 1
    detail = demo_client.get(f"/employers/{acme_id}/imports/{imp['import_id']}").json()
    assert detail["status"] == "COMMITTED" and detail["run_id"] == run["id"]


def test_import_commit_only_ok_rows(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    csv_text = ("name,amount,currency,iban\n"
                "Good One,100,EUR,DE89370400440532013000\n"
                "Bad One,-5,EUR,DE89370400440532013001\n")
    imp = demo_client.post(f"/employers/{acme_id}/imports/preview",
                           json={"filename": "mix.csv", "content": csv_text}).json()
    run = demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit", json={}).json()
    assert [p["beneficiary_name"] for p in run["pay_lines"]] == ["Good One"]


def test_import_mapping_override_and_cross_tenant_404(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    csv_text = "worker,pay_amt,ccy,bank\nZed Zee,42,GBP,GB29NWBK60161331926819\n"
    body = demo_client.post(f"/employers/{acme_id}/imports/preview", json={
        "filename": "mapped.csv", "content": csv_text,
        "mapping": {"worker": "beneficiary_name", "pay_amt": "amount", "ccy": "currency", "bank": "iban"}}).json()
    assert body["valid_count"] == 1, body
    assert body["rows"][0]["data"]["beneficiary_name"] == "Zed Zee"
    # Another employer's import id is invisible.
    other_id = _employers(demo_client)[BOREALIS]["id"]
    assert demo_client.get(f"/employers/{other_id}/imports/{body['import_id']}").status_code == 404
    assert demo_client.post(f"/employers/{other_id}/imports/{body['import_id']}/commit", json={}).status_code == 404
    assert demo_client.post("/employers/00000000-0000-0000-0000-000000000000/imports/preview",
                            json={"filename": "x", "content": CSV_OK}).status_code == 404


# ------------------------------------------------------- ledger / funding

def test_ledger_entries_are_employer_scoped(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    borealis_id = _employers(demo_client)[BOREALIS]["id"]
    rows = demo_client.get(f"/employers/{acme_id}/ledger-entries").json()
    assert rows, "settled Acme must have ledger entries"
    assert all(r["account"].startswith(f"emp_{acme_id.replace('-', '')}:") for r in rows)
    assert all(set(r) >= {"id", "transaction_id", "account", "amount", "currency", "memo", "created_at"} for r in rows)
    usd = demo_client.get(f"/employers/{acme_id}/ledger-entries", params={"currency": "USD"}).json()
    assert usd and all(r["currency"] == "USD" for r in usd)
    page = demo_client.get(f"/employers/{acme_id}/ledger-entries", params={"limit": 2, "offset": 0}).json()
    assert len(page) == 2
    # Borealis has only its seed deposit in its own namespace.
    brows = demo_client.get(f"/employers/{borealis_id}/ledger-entries").json()
    assert brows and all(r["account"].startswith(f"emp_{borealis_id.replace('-', '')}:") for r in brows)
    assert {r["transaction_id"] for r in brows}.isdisjoint({r["transaction_id"] for r in rows})


def test_funding_history_from_ledger_deposits(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    cobalt_id = _employers(demo_client)[COBALT]["id"]
    hist = demo_client.get(f"/employers/{acme_id}/funding-history").json()
    assert len(hist) == 1 and hist[0]["amount"] == "20000.00" and hist[0]["currency"] == "USD"
    assert hist[0]["source"] == "deposit" and hist[0]["status"] == "confirmed"
    # Cobalt was never funded: honestly empty, no invented rows.
    assert demo_client.get(f"/employers/{cobalt_id}/funding-history").json() == []
    demo_client.post(f"/employers/{cobalt_id}/fund", json={"amount": "750", "reference": "topup-1"})
    hist2 = demo_client.get(f"/employers/{cobalt_id}/funding-history").json()
    assert len(hist2) == 1 and hist2[0]["amount"] == "750.00"


# ------------------------------------------------------------ journals

def test_journals_balanced_and_platform_book(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    journals = demo_client.get(f"/employers/{acme_id}/journals").json()
    assert journals
    for j in journals:
        assert j["balanced"] is True, j
        per_ccy = {}
        for line in j["lines"]:
            per_ccy[line["currency"]] = per_ccy.get(line["currency"], 0.0) + float(line["debit"]) - float(line["credit"])
        assert all(abs(v) < 0.005 for v in per_ccy.values())
    platform = demo_client.get("/platform/journals").json()
    assert platform and all(j["balanced"] for j in platform)
    fee_lines = [line for j in platform for line in j["lines"] if line["account"].startswith("platform:fees:")]
    assert fee_lines and sum(float(l["debit"]) for l in fee_lines) == 149.00


def test_journals_export_downloads(demo_client):
    _settle_acme(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    r = demo_client.get(f"/employers/{acme_id}/journals/export", params={"format": "csv"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "account" in r.text and "platform run fee" in r.text
    r = demo_client.get(f"/employers/{acme_id}/journals/export", params={"format": "json"})
    assert "attachment" in r.headers["content-disposition"]
    assert json.loads(r.text)


# -------------------------------------------------------- reconciliation

def test_reconciliation_math_against_settled_acme(demo_client):
    done = _settle_acme(demo_client)
    assert done["fee_charged_usd"] == "149.00"  # golden fee, exactly 149
    acme_id = _employers(demo_client)[ACME]["id"]
    rec = demo_client.get(f"/employers/{acme_id}/reconciliation").json()
    by_ccy = {r["currency"]: r for r in rec["currencies"]}
    usd, eur = by_ccy["USD"], by_ccy["EUR"]
    assert usd["deposits"] == "20000.00"
    assert usd["fx_sells"] == "6507.76"
    assert usd["platform_charges"] == "149.00"
    assert usd["closing_cash"] == "13343.24"
    assert eur["fx_buys"] == "6025.70"
    assert eur["payout_debits"] == "6000.00"
    assert eur["provider_fees"] == "25.70"
    assert eur["closing_cash"] == "0.00"
    for row in rec["currencies"]:
        assert row["variance"] == "0.00" and row["status"] == "matched"
        identity = (float(row["opening_cash"]) + float(row["deposits"]) + float(row["refunds"])
                    + float(row["fx_buys"]) - float(row["fx_sells"]) - float(row["payout_debits"])
                    - float(row["provider_fees"]) - float(row["platform_charges"]) + float(row["other"]))
        assert abs(identity - float(row["closing_cash"])) < 0.005
    assert rec["matched_percent"] == 100.0
    assert len(rec["settlements"]) == 2
    assert all(s["matched"] and s["provider_ref"] for s in rec["settlements"])


def test_reconciliation_pending_for_held_borealis(demo_client):
    _hold_borealis(demo_client)
    borealis_id = _employers(demo_client)[BOREALIS]["id"]
    rec = demo_client.get(f"/employers/{borealis_id}/reconciliation").json()
    usd = {r["currency"]: r for r in rec["currencies"]}["USD"]
    assert usd["deposits"] == "5000.00" and usd["closing_cash"] == "5000.00"
    assert usd["status"] == "pending_evidence"  # held pay line = unmatched settlement
    assert rec["matched_percent"] == 0.0


# -------------------------------------------------------------- evidence

def test_evidence_bundle_complete_for_settled_run(demo_client):
    done = _settle_acme(demo_client)
    r = demo_client.get(f"/runs/{done['id']}/evidence")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == f'attachment; filename="evidence-{done["id"]}.json"'
    bundle = json.loads(r.text)
    assert bundle["manifest"]["version"] and bundle["manifest"]["generated_at"]
    assert bundle["manifest"]["mode"] == "demo"
    assert bundle["employer"]["name"] == ACME
    assert bundle["run"]["id"] == done["id"] and bundle["run"]["status"] == "SETTLED"
    assert bundle["run"]["assessment"]["total_required"] == "6025.70"
    assert bundle["approvals"] and bundle["approvals"][0]["verified"] is True
    assert bundle["tap"]["verified_count"] == 1 and bundle["tap"]["all_verified"] is True
    assert len(bundle["pay_lines"]) == 2
    assert all(p["airwallex_transfer_id"] for p in bundle["pay_lines"])
    assert bundle["agent_events"]
    assert bundle["metal"]["receipt"]["receipt_hash"]
    assert bundle["metal"]["tx_hash"] == bundle["metal"]["receipt"]["tx_hash"]
    assert bundle["journal_transaction_ids"]
    assert bundle["exceptions"] == []
    assert any("simulator" in s for s in bundle["limitations"])


def test_evidence_cross_tenant_404(demo_client):
    done = _settle_acme(demo_client)
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Other Org", "country": "GB", "base_currency": "USD"},
        "admin": {"full_name": "Olive Other", "email": "olive@other.io", "password": "Other2026x"},
    })
    assert demo_client.get(f"/runs/{done['id']}/evidence").status_code == 404


# ------------------------------------------------------------ exceptions

def test_exceptions_derived_held_import_and_security(demo_client):
    held = _hold_borealis(demo_client)
    acme_id = _employers(demo_client)[ACME]["id"]
    bad_csv = "name,amount,currency,iban\nBroken,-1,EUR,DE89370400440532013000\n"
    demo_client.post(f"/employers/{acme_id}/imports/preview", json={"filename": "bad.csv", "content": bad_csv})
    # A chat security event, written the way the chat guard writes it.
    db = demo_client.app.state.db
    with db.sessionmaker() as s:
        from sqlalchemy import select

        from app.db.models import ChatSecurityEvent, Organization
        org = s.scalars(select(Organization)).first()
        s.add(ChatSecurityEvent(organization_id=org.id, kind="cross_tenant",
                                detail="Attempted to reference another employer's run."))
        s.commit()

    exc = demo_client.get("/exceptions").json()
    types = {e["type"] for e in exc}
    assert "shortfall" in types and "import_errors" in types and "chat_security" in types
    short = next(e for e in exc if e["type"] == "shortfall")
    assert short["run_id"] == held["id"] and short["employer_name"] == BOREALIS
    assert "2642.88" in short["title"]
    assert all(set(e) >= {"id", "type", "severity", "employer_id", "employer_name", "run_id",
                          "title", "detail", "owner", "next_action", "evidence_label", "created_at"}
               for e in exc)


def test_exceptions_empty_for_fresh_org(demo_client):
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Clean Org", "country": "GB", "base_currency": "USD"},
        "admin": {"full_name": "Cleo Clean", "email": "cleo@clean.io", "password": "Clean2026x"},
    })
    assert demo_client.get("/exceptions").json() == []


# -------------------------------------------------------------- calendar

def test_calendar_derived_from_pay_date(demo_client):
    acme_id = _employers(demo_client)[ACME]["id"]
    imp = demo_client.post(f"/employers/{acme_id}/imports/preview",
                           json={"filename": "nov.csv", "content": CSV_OK}).json()
    demo_client.post(f"/employers/{acme_id}/imports/{imp['import_id']}/commit",
                     json={"period_label": "Nov 2026", "pay_date": "2026-11-30"})
    events = demo_client.get(f"/employers/{acme_id}/calendar").json()
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["pay_date"]["date"] == "2026-11-30"
    assert by_kind["cutoff"]["date"] == "2026-11-28"
    assert by_kind["funding_due"]["date"] == "2026-11-27"
    assert by_kind["period_start"]["date"] == "2026-11-01"
    assert all(e["derived"] is True for e in events)
    # Employers whose runs carry no pay_date produce no invented events.
    cobalt_id = _employers(demo_client)[COBALT]["id"]
    assert demo_client.get(f"/employers/{cobalt_id}/calendar").json() == []


# --------------------------------------------------------------- receipts

def _pay_line_id(c, run_id, index=0):
    import uuid as _uuid

    db = c.app.state.db
    with db.sessionmaker() as s:
        from sqlalchemy import select

        from app.db.models import PayLine
        line = s.scalars(select(PayLine).where(PayLine.run_id == _uuid.UUID(str(run_id)))
                         .order_by(PayLine.position)).all()[index]
        return str(line.id)


def test_worker_receipt_token_flow_and_masking(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    r = demo_client.get(f"/runs/{done['id']}/pay-lines/{line_id}/receipt-token")
    assert r.status_code == 200, r.text
    token, path = r.json()["token"], r.json()["path"]
    assert len(token) == 32 and path == f"/receipts/{line_id}?token={token}"

    # Public receipt works without any session.
    demo_client.post("/auth/logout")
    receipt = demo_client.get(path)
    assert receipt.status_code == 200, receipt.text
    body = receipt.json()
    assert body["beneficiary_name"] == "Lena Fischer"
    assert body["amount"] == "3400.00" and body["currency"] == "EUR"
    assert body["status"] == "PAID" and body["employer_name"] == ACME
    assert body["bank_masked"] == "****3000"  # Lena's IBAN ends 3000; nothing more leaks
    assert "DE89370400440532013000" not in receipt.text
    assert body["verification"]["tap_verified"] is True
    assert body["settled_at"] is not None and body["timeline"]


def test_worker_receipt_wrong_token_is_404(client):
    # Unauthenticated client + demo data via its own login first.
    from app.services.seed import DEMO_EMAIL, DEMO_PASSWORD

    client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    done = _settle_acme(client)
    line_id = _pay_line_id(client, done["id"], 0)
    client.post("/auth/logout")
    assert client.get(f"/receipts/{line_id}?token={'0' * 32}").status_code == 404
    assert client.get(f"/receipts/{line_id}").status_code == 404
    assert client.get("/receipts/00000000-0000-0000-0000-000000000000?token=" + "0" * 32).status_code == 404


def test_receipt_token_requires_tenant_ownership(demo_client):
    done = _settle_acme(demo_client)
    line_id = _pay_line_id(demo_client, done["id"], 0)
    demo_client.post("/auth/logout")
    demo_client.post("/auth/signup", json={
        "organization": {"name": "Third Org", "country": "GB", "base_currency": "USD"},
        "admin": {"full_name": "Tess Third", "email": "tess@third.io", "password": "Third2026x"},
    })
    assert demo_client.get(f"/runs/{done['id']}/pay-lines/{line_id}/receipt-token").status_code == 404
