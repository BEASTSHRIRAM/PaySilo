from app.agents.demo import run_demo


def test_full_demo_scenario():
    report = run_demo()

    acme = report["employers"]["acme"]
    assert acme["status"] == "SETTLED"
    assert acme["assessment"]["total_required"] == "6025.70"
    assert acme["fee_charged_usd"] == "149.00"
    assert acme["metal_receipt"] is not None
    assert acme["metal_receipt"]["chain"] == "mock"

    borealis = report["employers"]["borealis"]
    assert borealis["status"] == "HELD_SHORTFALL"
    assert borealis["assessment"]["shortfall_usd"] == "1493.88"
    assert borealis["fee_charged_usd"] == "0.00"  # no fee for a payroll that never ran

    # Cobalt's mid-run deposit did not unblock Borealis.
    assert report["cobalt"]["deposit_usd"] == "10000.00"
    assert report["borealis_recheck"]["can_run"] is False
    assert report["borealis_recheck"]["shortfall_usd"] == "1493.88"
    assert "tenant-scoped" in report["cross_tenant_guard"]
    assert any("tenant-scoped" in line for line in report["audit"])

    # Platform ends with exactly the one fee it earned. No tenant money touched.
    assert report["platform"]["fees_collected_usd"] == "149.00"
    assert report["settlement_chain_valid"] is True
    assert report["ledger_balanced"] is True
    assert len(report["metal_receipts"]) == 1


def test_demo_is_repeatable():
    first = run_demo()
    second = run_demo()
    assert first["employers"]["acme"]["status"] == second["employers"]["acme"]["status"]
    assert first["platform"]["fees_collected_usd"] == second["platform"]["fees_collected_usd"]
