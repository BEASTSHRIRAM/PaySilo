"""The Kit 6 demo scenario, runnable end-to-end with NO API keys.

Employers (Vertex Payroll Services' clients):
- Acme Manufacturing GmbH — funds USD 20,000; pays Lena Fischer EUR 3,400
  and Tomas Rivera EUR 2,600. Total required EUR 6,025.70 -> SETTLED.
- Borealis Labs AB — funds USD 5,000; pays Ingrid Solberg EUR 6,000.
  Required EUR 6,012.85 ≈ USD 6,493.88, plus the USD 149 run fee = USD
  6,642.88 leaving the wallet, and a USD 1,000 reserve floor must stay put
  -> short by USD 2,642.88 -> HELD_SHORTFALL, escalated to a human.
- Cobalt Retail LLC — no payroll due; receives a USD 10,000 deposit
  mid-run. Its money must NOT unblock Borealis (cross-tenant guard).

Demo FX rate: 1 EUR = 1.08 USD. Platform ends with exactly USD 149 in fees
(only Acme's payroll ran, so only Acme is charged).
"""

from __future__ import annotations

from decimal import Decimal

from langgraph.types import Command

from ..airwallex.simulator import USD_VALUE, AirwallexSimulator, request_id_for
from ..ledger import Ledger
from ..money import money_str, to_decimal
from ..partners.metal import MockSettlementChain
from ..policy import ContractorPayroll, assess_payroll, cross_tenant_guard
from .graph import Services, build_graph, initial_state

EMPLOYERS: dict[str, dict] = {
    "acme": {
        "name": "Acme Manufacturing GmbH", "contact": "Ana Weber", "funding_usd": "20000",
        "pay_lines": [
            {"name": "Lena Fischer", "amount": "3400", "currency": "EUR", "method": "SWIFT",
             "iban": "DE89370400440532013000", "swift_code": "COBADEFFXXX", "country_code": "DE"},
            {"name": "Tomas Rivera", "amount": "2600", "currency": "EUR", "method": "SWIFT",
             "iban": "ES9121000418450200051332", "swift_code": "CAIXESBBXXX", "country_code": "ES"},
        ],
    },
    "borealis": {
        "name": "Borealis Labs AB", "contact": "Bo Lindqvist", "funding_usd": "5000",
        "pay_lines": [{"name": "Ingrid Solberg", "amount": "6000", "currency": "EUR", "method": "SWIFT",
                       "iban": "SE4550000000058398257466", "swift_code": "ESSESESSXXX", "country_code": "SE"}],
    },
    "cobalt": {
        "name": "Cobalt Retail LLC", "contact": "Cleo Nakamura", "funding_usd": "0",
        "pay_lines": [],
    },
}

COBALT_MID_RUN_DEPOSIT = Decimal("10000")


def build_demo_services() -> Services:
    return Services(
        airwallex=AirwallexSimulator(),
        ledger=Ledger(),
        chain=MockSettlementChain(),
        usd_rates=dict(USD_VALUE),
    )


DEMO_APPROVER = "demo-operator@vertexpayroll.example"


def demo_resume(services: Services, state: dict, interrupt_payload: dict) -> dict:
    """The demo's deterministic human: approve good payroll, hold shortfalls.

    Approvals are Visa TAP-signed exactly like a real operator's would be,
    over the run's own amounts and pay lines.
    """
    if interrupt_payload.get("kind") == "approval":
        envelope = services.tap.sign_approval(
            run_id=state["run_id"], employer_key=state["employer_key"], decision="approve",
            assessment=state["assessment"], pay_lines=state["pay_lines"], decided_by=DEMO_APPROVER,
            held_lines=state.get("held_lines") or [])
        return {"decision": "approve", "decided_by": DEMO_APPROVER, "tap": envelope.as_dict()}
    return {"decision": "hold", "decided_by": DEMO_APPROVER}


def drive_run(graph, services: Services, state: dict, run_id: str) -> dict:
    """Invoke a run, answering interrupts with the demo human's decisions."""
    config = {"configurable": {"thread_id": run_id}}
    result = graph.invoke(state, config)
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        current = graph.get_state(config).values
        result = graph.invoke(Command(resume=demo_resume(services, current, payload)), config)
    return graph.get_state(config).values


def run_demo(services: Services | None = None) -> dict:
    services = services or build_demo_services()
    sim: AirwallexSimulator = services.airwallex  # type: ignore[assignment]
    ledger, chain = services.ledger, services.chain
    graph = build_graph(services)

    audit: list[str] = []
    results: dict[str, dict] = {}
    account_ids: dict[str, str] = {}

    for key in ("acme", "borealis"):
        spec = EMPLOYERS[key]
        run_id = f"demo-{key}"
        final = drive_run(graph, services, initial_state(
            run_id=run_id, employer_key=key, employer_name=spec["name"],
            contact_name=spec["contact"], funding_usd=spec["funding_usd"],
            pay_lines=spec["pay_lines"],
        ), run_id)
        account_ids[key] = final["account_id"]
        audit.extend(final["audit"])
        results[key] = {
            "employer": spec["name"], "status": final["status"],
            "assessment": final["assessment"], "fee_charged_usd": final["fee_charged_usd"],
            "explanation": final["explanation"], "metal_receipt": final["metal_receipt"],
            "balances": final["balances"], "events": final.get("events", []),
            "signatures": final.get("signatures", []),
        }

    # -- Cobalt: deposit lands mid-run; Borealis must stay short -------------
    cobalt = EMPLOYERS["cobalt"]
    cobalt_account = sim.create_connected_account(
        request_id=request_id_for("demo-cobalt", "connected-account"),
        business_name=cobalt["name"], contact_name=cobalt["contact"],
    )
    account_ids["cobalt"] = cobalt_account["id"]
    sim.deposit(request_id=request_id_for("demo-cobalt", "mid-run-deposit"),
                account_id=cobalt_account["id"], amount=COBALT_MID_RUN_DEPOSIT, currency="USD")
    ledger.post(
        [("cobalt:cash:USD", COBALT_MID_RUN_DEPOSIT), ("external:bank:USD", -COBALT_MID_RUN_DEPOSIT)],
        memo="demo-cobalt mid-run deposit",
    )
    audit.append(f"[Cobalt Retail LLC] Deposit of USD {money_str(COBALT_MID_RUN_DEPOSIT)} landed mid-run in Cobalt's own wallet.")

    borealis_balances = sim.get_balances(account_id=account_ids["borealis"])
    recheck = assess_payroll(
        [ContractorPayroll(name=l["name"], amount=to_decimal(l["amount"]), currency=l["currency"],
                           method=l.get("method", "SWIFT"))
         for l in EMPLOYERS["borealis"]["pay_lines"]],
        services.usd_rates, borealis_balances,
    )
    guard_line = cross_tenant_guard("Borealis Labs AB", "Cobalt Retail LLC")
    audit.append(
        f"[Borealis Labs AB] Re-check after Cobalt's deposit: Borealis still holds USD "
        f"{money_str(recheck.available_usd)} and is still short USD {money_str(recheck.shortfall_usd)}."
    )
    audit.append(f"[Guard] {guard_line}")

    platform_balances = sim.get_balances()
    return {
        "scenario": "Starter Kit 6 — Multi-Employer Payroll Executor (demo, simulator, no API keys)",
        "fx_usd_value": {c: money_str(v) for c, v in services.usd_rates.items()},
        "employers": results,
        "cobalt": {
            "employer": cobalt["name"], "deposit_usd": money_str(COBALT_MID_RUN_DEPOSIT),
            "balances": {c: money_str(v) for c, v in sim.get_balances(account_id=cobalt_account["id"]).items()},
            "payroll_status": "NO_PAYROLL_DUE",
        },
        "borealis_recheck": {
            "available_usd": money_str(recheck.available_usd),
            "required_usd": money_str(recheck.required_usd),
            "shortfall_usd": money_str(recheck.shortfall_usd),
            "can_run": recheck.can_run,
        },
        "cross_tenant_guard": guard_line,
        "platform": {
            "balances": {c: money_str(v) for c, v in platform_balances.items()},
            "fees_collected_usd": money_str(platform_balances.get("USD", Decimal("0"))),
        },
        "metal_receipts": [
            {"index": r.index, "payload_hash": r.payload_hash, "prev_hash": r.prev_hash,
             "receipt_hash": r.receipt_hash, "chain": r.chain}
            for r in chain.receipts  # type: ignore[attr-defined]
        ],
        "settlement_chain_valid": chain.verify_chain(),
        "ledger_balanced": ledger.is_balanced(),
        "audit": audit,
    }
