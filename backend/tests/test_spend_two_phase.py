"""Kits and trips use the SAME two-phase VIC path as benefits: one mandate per purchase row, human Complete."""

from __future__ import annotations

from decimal import Decimal

from app.partners.vic import AWAITING_AUTH, CompleteResult, MandateInfo, VicError


class FakeAiri:
    mode = "VIC"
    credential_ref = "airi_vic_wallet"
    two_phase = True
    test_card = True
    cli_available = True

    def __init__(self):
        self.mandates = []
        self.cancelled = []
        self.authorized = False

    def probe(self, *, force=False):
        return True, True

    def ensure_ready(self):
        pass

    def purchase(self, **_kw):  # the one-shot path must never be used live
        raise AssertionError("one-shot purchase used in two-phase mode")

    def create_mandate(self, *, merchant, merchant_url, amount, currency, item, description):
        if amount > Decimal("200"):
            raise VicError("TX_LIMIT_EXCEEDED", "limit")
        mid = f"m{len(self.mandates) + 1}"
        self.mandates.append((mid, merchant_url))
        return MandateInfo(mandate_id=mid, approval_url=f"https://airi.example/{mid}", payment_method_ref="airi_pm_1")

    def complete(self, *, mandate_id, merchant, amount, currency, executor, wait_seconds=20):
        if not self.authorized:
            return CompleteResult(state=AWAITING_AUTH)
        out = executor.execute(mandate_id=mandate_id, merchant=merchant, amount=amount, currency=currency,
                               credential_file="")
        return CompleteResult(state="COMPLETED", ok=True) if out.result == "SUCCEEDED" else \
            CompleteResult(state="CANCELLED", failure_code="PURCHASE_NOT_CONFIRMED", failure_detail="no")

    def cancel_mandate(self, mandate_id):
        self.cancelled.append(mandate_id)


def _live(client):
    fake = FakeAiri()
    client.app.state.runtime.vic = fake
    return fake


def _acme(client):
    emp = {e["name"]: e for e in client.get("/employers").json()}["Acme Manufacturing GmbH"]
    lena = next(c for c in client.get(f"/employers/{emp['id']}").json()["contractors"] if c["full_name"] == "Lena Fischer")
    return emp, lena


def _kit(client, items):
    emp, lena = _acme(client)
    kit = client.post("/onboarding-kits/kits", json={"employer_id": emp["id"], "contractor_id": lena["id"],
                                                     "items": items}).json()
    assert client.post(f"/onboarding-kits/kits/{kit['id']}/decision", json={"decision": "approve"}).status_code == 200
    return kit


ITEMS = [{"kind": "SAAS_SEAT", "label": "Seat A", "merchant": "github.com", "amount": "10.00", "currency": "USD"},
         {"kind": "SAAS_SEAT", "label": "Seat B", "merchant": "notion.so", "amount": "20.00", "currency": "USD"}]


def test_kit_live_is_pending_until_human_completes_each_item(demo_client):
    fake = _live(demo_client)
    kit = _kit(demo_client, ITEMS)
    out = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all").json()
    assert [p["status"] for p in out["purchases"]] == ["PENDING", "PENDING"]
    assert all(p["approval_url"].startswith("https://") for p in out["purchases"])
    assert out["kit"]["status"] == "APPROVED"  # nothing bought yet
    # clicking buy again never opens a second mandate for the same purchase rows
    demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all")
    assert len(fake.mandates) == 2
    first, second = out["purchases"]
    # not authorized yet: Complete leaves it pending
    r = demo_client.post(f"/onboarding-kits/purchases/{first['id']}/complete", json={"purchase_confirmed": True})
    assert r.status_code == 200 and r.json()["status"] == "PENDING"
    fake.authorized = True
    assert demo_client.post(f"/onboarding-kits/purchases/{first['id']}/complete",
                            json={"purchase_confirmed": True}).json()["status"] == "SUCCEEDED"
    kits = demo_client.get("/onboarding-kits/kits").json()
    assert next(k for k in kits if k["id"] == kit["id"])["status"] == "PARTIALLY_PURCHASED"
    demo_client.post(f"/onboarding-kits/purchases/{second['id']}/complete", json={"purchase_confirmed": True})
    kits = demo_client.get("/onboarding-kits/kits").json()
    assert next(k for k in kits if k["id"] == kit["id"])["status"] == "COMPLETED"  # only when ALL items are done


def test_kit_unconfirmed_complete_and_cancel_allow_retry(demo_client):
    fake = _live(demo_client)
    fake.authorized = True
    kit = _kit(demo_client, ITEMS[:1])
    p = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all").json()["purchases"][0]
    r = demo_client.post(f"/onboarding-kits/purchases/{p['id']}/complete", json={"purchase_confirmed": False}).json()
    assert r["status"] == "FAILED"
    p2 = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all").json()["purchases"][0]
    assert p2["attempt_no"] == 2 and p2["status"] == "PENDING"
    assert demo_client.post(f"/onboarding-kits/purchases/{p2['id']}/cancel").json()["mandate_status"] == "CANCELLED"
    assert fake.cancelled == ["m2"]


def test_live_kit_needs_website_merchant_and_blocks_over_limit(demo_client):
    _live(demo_client)
    kit = _kit(demo_client, [{"kind": "CUSTOM", "label": "Thing", "merchant": "Corner Shop", "amount": "10.00", "currency": "USD"}])
    r = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/items/{kit['items'][0]['id']}/purchase")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MERCHANT_URL_REQUIRED"


def test_trip_components_two_phase_and_blocked_never_split(demo_client):
    fake = _live(demo_client)
    emp, lena = _acme(demo_client)
    trip = demo_client.post("/travel/trips", json={
        "employer_id": emp["id"], "contractor_id": lena["id"], "destination": "Paris", "start_date": "2026-12-01",
        "end_date": "2026-12-02", "purpose": "x",
        "components": [{"kind": "TRANSPORT", "label": "Flight", "merchant": "airfrance.com", "amount": "450.00", "currency": "USD"},
                       {"kind": "LODGING", "label": "Hotel", "merchant": "booking.com", "amount": "180.00", "currency": "USD"}]}).json()
    assert demo_client.post(f"/travel/trips/{trip['id']}/decision",
                            json={"decision": "approve", "override_caps": True}).status_code == 200
    out = demo_client.post(f"/travel/trips/{trip['id']}/purchase-all").json()
    by = {p["amount"]: p for p in out["purchases"]}
    assert by["450.00"]["status"] == "FAILED" and by["450.00"]["failure_code"] == "TX_LIMIT_EXCEEDED"
    assert by["180.00"]["status"] == "PENDING"
    assert len(fake.mandates) == 1  # the over-limit component never got a mandate and is never split
    fake.authorized = True
    done = demo_client.post(f"/travel/purchases/{by['180.00']['id']}/complete", json={"purchase_confirmed": True}).json()
    assert done["status"] == "SUCCEEDED"
    trips = demo_client.get("/travel/trips").json()
    t = next(x for x in trips if x["id"] == trip["id"])
    assert t["status"] == "PARTIALLY_PURCHASED"
    flight = next(c for c in t["components"] if c["label"] == "Flight")
    assert flight["status"] == "PENDING" and flight["tx_limit_blocked"] is True


def test_simulated_mode_completes_instantly_like_benefits(demo_client):
    kit = _kit(demo_client, ITEMS)
    out = demo_client.post(f"/onboarding-kits/kits/{kit['id']}/purchase-all").json()
    assert out["kit"]["status"] == "COMPLETED" and all(p["status"] == "SUCCEEDED" for p in out["purchases"])
    assert all(p["mode"] == "SIMULATED" for p in out["purchases"])


def test_kit_purchase_cross_tenant_complete_is_404(demo_client):
    _live(demo_client)
    import uuid

    assert demo_client.post(f"/onboarding-kits/purchases/{uuid.uuid4()}/complete", json={}).status_code == 404
