"""Benefits budgets: every employer is listed, and a missing budget is caught at approval, not at purchase."""


def _employer(client, name):
    return next(e for e in client.get("/employers").json() if e["name"].startswith(name))


def _new_request(client, employer_id, merchant="Example Store"):
    r = client.post("/benefits/requests", json={
        "employer_id": employer_id, "contractor_name": "Test Person", "merchant": merchant,
        "item": "Course", "amount": "49.99", "currency": "USD"})
    assert r.status_code == 201, r.text
    return r.json()


def test_every_employer_gets_a_budget_card_and_saving_creates_it(demo_client):
    employers = demo_client.get("/employers").json()
    budgets = demo_client.get("/benefits/budgets").json()
    assert {b["employer_id"] for b in budgets} == {e["id"] for e in employers}
    missing = [b for b in budgets if b["id"] is None]
    assert missing, "the demo seed leaves at least one employer without a benefits budget"
    target = missing[0]
    assert target["enabled"] is False and target["monthly_cap"] == "0.00"

    req = _new_request(demo_client, target["employer_id"])
    r = demo_client.post(f"/benefits/requests/{req['id']}/decision", json={"decision": "approve"})
    assert r.status_code in (409, 422), r.text
    assert "BUDGET_NOT_ENABLED" in r.text
    assert demo_client.get("/benefits/requests").json()[0]["status"] == "REQUESTED"  # still decidable

    r = demo_client.put(f"/benefits/budgets/{target['employer_id']}",
                        json={"currency": "USD", "monthly_cap": "300.00", "per_tx_limit": None, "enabled": True})
    assert r.status_code == 200 and r.json()["id"] is not None
    after = [b for b in demo_client.get("/benefits/budgets").json() if b["employer_id"] == target["employer_id"]]
    assert len(after) == 1 and after[0]["enabled"] is True  # the placeholder is replaced, not duplicated

    r = demo_client.post(f"/benefits/requests/{req['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
