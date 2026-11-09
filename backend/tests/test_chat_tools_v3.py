"""Chat tool layer: real arg schemas, safe dispatch, exact employer resolution (no LLM involved)."""

import pytest
from sqlalchemy import select

from app.chat import service as chat_service
from app.chat import tools as chat_tools
from app.db.models import Employer, Membership, Organization, User


@pytest.fixture
def ctx_factory(demo_client):
    db = demo_client.app.state.db
    runtime = demo_client.app.state.runtime
    sessions = []

    def make():
        s = db.sessionmaker()
        sessions.append(s)
        membership = s.scalars(select(Membership)).first()
        org = s.get(Organization, membership.organization_id)
        user = s.get(User, membership.user_id)
        return chat_tools.ChatContext(db=s, runtime=runtime, organization_id=org.id, organization_name=org.name,
                                      user_id=user.id, user_email=user.email, role=membership.role,
                                      demo_mode=True)
    yield make
    for s in sessions:
        s.close()


def test_each_llm_tool_exposes_its_real_parameters_not_kwargs(ctx_factory):
    tools, wrappers = chat_service.build_llm_tools(ctx_factory())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == set(chat_tools.TOOL_FUNCTIONS) and set(wrappers) == set(by_name)
    for t in tools:
        props = t.args_schema.model_json_schema().get("properties", {})
        assert "kwargs" not in props and "ctx" not in props
    assert set(by_name["demo_fund_employer"].args_schema.model_json_schema()["properties"]) == {"employer", "amount"}
    assert set(by_name["explain_run"].args_schema.model_json_schema()["properties"]) == {"run_id", "employer"}
    assert set(by_name["list_employers"].args_schema.model_json_schema().get("properties", {})) == set()


def test_run_tool_ignores_unknown_and_local_variable_arguments(ctx_factory):
    ctx = ctx_factory()
    # 'rows' / 'emp' are LOCAL variables of the tool functions; they used to pass the co_varnames filter.
    result = chat_service._run_tool(ctx, "get_employer_balance",
                                    {"employer": "Acme Manufacturing GmbH", "rows": [], "emp": "x", "bogus": 1})
    assert "Acme Manufacturing GmbH" in result.text
    assert chat_service._run_tool(ctx, "list_employers", {"anything": 1}).data["employers"]


def test_unknown_tool_name_is_a_friendly_result_not_a_keyerror(ctx_factory):
    result = chat_service._run_tool(ctx_factory(), "wire_money", {"amount": "1"})
    assert "do not have a tool called 'wire_money'" in result.text
    assert result.data["error"] == "unknown_tool"


def test_structured_tool_invocation_drops_unknown_args(ctx_factory):
    tools, _ = chat_service.build_llm_tools(ctx_factory())
    tool = next(t for t in tools if t.name == "get_employer_balance")
    out = tool.invoke({"employer": "Acme Manufacturing GmbH"})
    assert "Acme Manufacturing GmbH" in out


def test_resolve_employer_needs_exact_name_or_id_not_a_substring(ctx_factory):
    ctx = ctx_factory()
    acme = next(e for e in chat_tools.employers(ctx) if e.name.startswith("Acme"))
    assert chat_tools.resolve_employer(ctx, "a") is None  # used to match the first employer containing "a"
    assert chat_tools.resolve_employer(ctx, "cme") is None
    assert chat_tools.resolve_employer(ctx, "ACME MANUFACTURING GMBH").id == acme.id
    assert chat_tools.resolve_employer(ctx, str(acme.id)).id == acme.id
    assert chat_tools.resolve_employer(ctx, "Acme").id == acme.id  # unique first word


def test_ambiguous_employer_asks_for_clarification_and_never_funds(ctx_factory):
    ctx = ctx_factory()
    org_id = ctx.organization_id
    ctx.db.add(Employer(organization_id=org_id, name="Acme Logistics BV", country="NL", status="ACTIVE"))
    ctx.db.commit()
    assert chat_tools.resolve_employer(ctx, "Acme") is None  # two candidates
    result = chat_tools.demo_fund_employer(ctx, employer="Acme", amount="500")
    assert "more than one employer" in result.text and "Acme Logistics BV" in result.text
    assert result.data["ambiguous"]
    # an exact name still resolves, and a substring still asks/refuses
    assert chat_tools.resolve_employer(ctx, "Acme Logistics BV") is not None
    assert "cannot find" in chat_tools.start_payroll_run(ctx, employer="a").text
