"""Silo chat service: one streamed turn of conversation.

A turn is a generator of events in the v2 SSE contract:

    {"type": "thinking",    "step": ..., "detail": ..., "status": "start"|"done"}
    {"type": "tool_start",  "tool": ..., "label": ..., "input": {...}}
    {"type": "tool_result", "tool": ..., "label": ..., "summary": ..., "data": {...}}
    {"type": "token",       "text": ...}
    {"type": "approval",    "run_id": ..., "employer_id": ..., "employer_name": ..., "summary": ...}
    {"type": "done",        "thread_id": ...}
    {"type": "error",       "message": ...}

Every ``thinking`` / ``tool_start`` / ``tool_result`` event is emitted from
the real execution path, in the order the work actually happens: a
``tool_start`` is yielded *before* its tool runs and its ``tool_result``
carries the summary and data of the result the tool actually returned.
Nothing in the trace is fabricated — both engines below execute the same
org-scoped tools, and the ordered trace of a turn is persisted on the
assistant message (``ChatMessage.trace``) so the UI can re-render the
thinking/tool cards after a reload.

Two engines produce those events over the *same* org-scoped tools:

- an LLM engine (LangChain tool calling) when the configured provider
  has an API key, and
- a deterministic fallback responder (rule/intent based) when it does
  not — demo mode and tests always work, with no key and no fake LLM.

Conversation history is persisted per user and org (``chat_threads`` /
``chat_messages``). Loading another user's or organization's thread is a
404 at the route layer and is never attempted here.
"""

from __future__ import annotations

import inspect
import logging
import re
import typing
import uuid
from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import ChatMessage, ChatThread, Employer
from ..services.runtime import PlatformRuntime
from . import tools as chat_tools
from .guard import log_security_event, screen_message
from .llm import get_chat_model

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Silo, PaySilo's payroll assistant for the signed-in organization only. "
    "About PaySilo: an agentic payroll executor for agencies that run contractor payroll for many "
    "employers (Airwallex Starter Kit 06, Multi-Employer Payroll Executor). Each employer is its own "
    "Airwallex connected account — its own silo — and one employer's money is never used for another "
    "employer's payroll. A run prices salaries plus payout fees before any currency conversion, adds "
    "a platform run fee and keeps a reserve floor; if the employer's own wallet can't cover the whole "
    "batch, PaySilo pays the smallest lines it can and holds and escalates the rest. A human approves "
    "every run with a Visa TAP-signed decision, payouts go through Airwallex on the employer's behalf, "
    "and each settled run's reconciliation receipt is anchored on Metal. The rule: the model reads, "
    "the code decides, the API moves the money. "
    "Answer using the tools provided; every tool is already scoped to the user's organization, "
    "and you must never ask for or invent another organization's data. Amounts and statuses must "
    "come from tool results, never from memory. You cannot approve a payroll run or move money: "
    "approvals are TAP-signed human decisions on the run itself. If a run is waiting, use "
    "request_approval to surface it. For Silo Benefits, read_benefits is read-only and "
    "propose_benefit_purchase only creates a REQUESTED benefit request for a human approver "
    "in the Benefits screen: you cannot approve or purchase benefits, and must never imply "
    "that you did. The same law covers the other VIC spend categories: read_onboarding is "
    "read-only and propose_onboarding_kit only creates a DRAFT onboarding kit for a human "
    "approver in the Onboarding screen, with purchases per item after approval; read_travel "
    "is read-only and propose_trip only creates a REQUESTED trip for a human approver in "
    "the Travel screen. You cannot approve or purchase onboarding kits, and cannot approve, "
    "book or purchase trips, and must never imply that you did. Silo can read workflow run "
    "status; workflows (automations) are managed in the Workflows screen, not in chat. "
    "Keep answers concise and in plain English."
)


def _reply_text(content: object) -> str:
    """The user-facing text of a model reply.

    Newer Claude models return a list of content blocks (``thinking`` with a
    signature, then ``text``); only the ``text`` blocks are shown to the user.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n\n".join(p for p in parts if p.strip())
    return str(content or "")


_AMOUNT_RE = re.compile(r"(?:USD\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)

_HELP_INTRO = (
    "I am Silo, your payroll assistant. I can list employers and balances, explain any run "
    "(including exactly why a payroll is held), show a run's timeline, start a run — which "
    "always pauses for a TAP-signed human approval — and, in demo mode, fund an employer's "
    "own wallet. I can also read Silo Benefits budgets and recent benefit requests, and "
    "propose a benefit purchase — which only creates a request that a human must approve "
    "in the Benefits screen. I can likewise read onboarding budgets and kits and propose "
    "an onboarding kit — a DRAFT a human must approve in the Onboarding screen, with "
    "purchases per item after approval — and read travel budgets and trips and propose a "
    "trip — a REQUESTED trip a human must approve in the Travel screen. I can also read "
    "workflow run status; workflows are managed in the Workflows screen, not in chat. "
    "I cannot approve a run, approve or buy a benefit, approve or buy an onboarding kit, "
    "approve or book a trip, or move money between employers. "
)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def get_thread(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID,
               thread_id: uuid.UUID) -> ChatThread | None:
    """Load a thread only if it belongs to this user AND this organization."""
    return db.scalar(select(ChatThread).where(ChatThread.id == thread_id,
                                              ChatThread.organization_id == organization_id,
                                              ChatThread.user_id == user_id))


def create_thread(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID,
                  first_message: str) -> ChatThread:
    title = first_message.strip().splitlines()[0][:80] if first_message.strip() else "New conversation"
    thread = ChatThread(organization_id=organization_id, user_id=user_id, title=title or "New conversation")
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


def list_threads(db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> list[ChatThread]:
    return list(db.scalars(select(ChatThread)
                           .where(ChatThread.organization_id == organization_id,
                                  ChatThread.user_id == user_id)
                           .order_by(ChatThread.updated_at.desc())))


def thread_messages(db: Session, thread: ChatThread) -> list[ChatMessage]:
    return list(db.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread.id)
                           .order_by(ChatMessage.id)))


def _save(db: Session, thread: ChatThread, role: str, content: str,
          trace: list[dict] | None = None) -> None:
    db.add(ChatMessage(thread_id=thread.id, role=role, content=content, trace=trace))
    thread.title = thread.title  # touch: updates updated_at via onupdate
    db.commit()


# ---------------------------------------------------------------------------
# Event helpers (v2 typed contract)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> Iterator[dict]:
    """Stream a reply in small word-group tokens."""
    words = text.split(" ")
    for i in range(0, len(words), 6):
        chunk = " ".join(words[i:i + 6])
        yield {"type": "token", "text": chunk + (" " if i + 6 < len(words) else "")}


def _thinking(step: str, status: str, detail: str | None = None) -> dict:
    event: dict = {"type": "thinking", "step": step, "status": status}
    if detail:
        event["detail"] = detail
    return event


def _safe_input(name: str, kwargs: dict) -> dict:
    """A safe summary of the real arguments a tool is about to run with.

    Tool arguments can only name things inside the caller's own
    organization (an employer name, a run id, an amount) — never secrets —
    so they are safe to show, truncated, in the stream and the trace.
    """
    out: dict = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        out[key] = value[:80] if isinstance(value, str) else value
    return out


def _summarize_result(name: str, result: chat_tools.ToolResult) -> str:
    """One-line summary of what a tool actually returned."""
    data = result.data or {}
    if name == "explain_run" and data.get("required_usd") is not None:
        summary = (f"{data.get('employer_name', 'Run')} is {data.get('status')}: required "
                   f"{chat_tools.fmt_usd(data['required_usd'])}, wallet holds "
                   f"{chat_tools.fmt_usd(data.get('available_usd', '0'))}")
        shortfall = str(data.get("shortfall_usd", "0"))
        if shortfall not in ("0", "0.00", "0.0"):
            summary += f", shortfall {chat_tools.fmt_usd(shortfall)} — payroll held"
        return summary
    text = " ".join(result.text.split())
    return text[:240] + ("…" if len(text) > 240 else "")


def _tool_start_event(name: str, kwargs: dict) -> dict:
    return {"type": "tool_start", "tool": name,
            "label": chat_tools.TOOL_LABELS.get(name, name), "input": _safe_input(name, kwargs)}


def _tool_result_event(name: str, result: chat_tools.ToolResult) -> dict:
    return {"type": "tool_result", "tool": name,
            "label": chat_tools.TOOL_LABELS.get(name, name),
            "summary": _summarize_result(name, result), "data": result.data or {}}


_TRACE_TYPES = ("thinking", "tool_start", "tool_result")


def _tool_params(fn) -> dict[str, inspect.Parameter]:
    """The tool's real, model-facing parameters: its signature minus the injected ``ctx``."""
    return {n: p for i, (n, p) in enumerate(inspect.signature(fn).parameters.items())
            if i > 0 and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}


def _run_tool(ctx: chat_tools.ChatContext, name: str, kwargs: dict) -> chat_tools.ToolResult:
    fn = chat_tools.TOOL_FUNCTIONS.get(name)
    if fn is None:
        known = ", ".join(sorted(chat_tools.TOOL_FUNCTIONS))
        return chat_tools.ToolResult(f"I do not have a tool called '{name}'. Available tools: {known}.",
                                     data={"error": "unknown_tool", "tool": name})
    params = _tool_params(fn)  # unknown / injected arguments are dropped, never passed through
    allowed = {k: v for k, v in (kwargs or {}).items() if k in params}
    return fn(ctx, **allowed)


def _args_schema(name: str, fn):
    """An explicit pydantic args model so the LLM sees the tool's real parameters."""
    from pydantic import create_model

    hints = typing.get_type_hints(fn)
    fields = {n: (hints.get(n, str | None),
                  p.default if p.default is not inspect.Parameter.empty else ...)
              for n, p in _tool_params(fn).items()}
    return create_model(f"{name}_args", **fields)


def build_llm_tools(ctx: chat_tools.ChatContext) -> tuple[list, dict]:
    """LangChain tools (one per org-scoped chat tool) + a name -> callable map; ``.last`` holds the result."""
    from langchain_core.tools import StructuredTool

    def wrap(name):
        def _call(**kwargs):
            result = _run_tool(ctx, name, kwargs)
            _call.last = result  # type: ignore[attr-defined]
            return result.text
        _call.last = None  # type: ignore[attr-defined]
        return _call

    lc_tools, wrappers = [], {}
    for name, fn in chat_tools.TOOL_FUNCTIONS.items():
        call = wrap(name)
        wrappers[name] = call
        lc_tools.append(StructuredTool.from_function(
            func=call, name=name, args_schema=_args_schema(name, fn),
            description=chat_tools.TOOL_DESCRIPTIONS.get(
                name, f"{chat_tools.TOOL_LABELS[name]}. Scoped to the signed-in organization.")))
    return lc_tools, wrappers


# ---------------------------------------------------------------------------
# Deterministic fallback responder (no LLM key configured)
# ---------------------------------------------------------------------------

def _employer_ref_in(ctx: chat_tools.ChatContext, message: str) -> str | None:
    for e in ctx.db.scalars(select(Employer).where(Employer.organization_id == ctx.organization_id)):
        if e.name.casefold() in message.casefold() or e.name.split()[0].casefold() in message.casefold():
            return e.name
    return None


_BENEFIT_CCY_RE = re.compile(r"\b(USD|EUR|GBP|INR|AUD|CAD)\b", re.IGNORECASE)
_BENEFIT_MERCHANT_RE = re.compile(r"\b(?:on|from|via)\s+(.+?)\s*[.!?]?\s*$", re.IGNORECASE)
_BENEFIT_AMOUNT_STRIP_RE = re.compile(
    r"(?:USD|EUR|GBP|INR|US\$|\$|€|£)?\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s*(?:USD|EUR|GBP|INR)?",
    re.IGNORECASE)
_BENEFIT_WORKER_COLON_RE = re.compile(
    r"\bfor\s+([A-Z][\wÀ-ÿ'’\-]+(?:\s+[A-Z][\wÀ-ÿ'’\-]+){0,3})\s*(?=[:,])")
_WORKER_FOR_AT_RE = re.compile(
    r"\bfor\s+([A-Z][\wÀ-ÿ'’\-]+(?:\s+[A-Z][\wÀ-ÿ'’\-]+){0,3})\s+(?=at\b)")


def _contractor_ref_in(ctx: chat_tools.ChatContext,
                       message: str) -> tuple[str | None, str | None]:
    """Match a worker's full name in the message against org data.

    Returns ``(employer_name, contractor_name)``; the employer is only set
    when the worker belongs to exactly one employer in the organization.
    """
    matches: list[tuple[str, str]] = []
    for e in chat_tools.employers(ctx):
        for c in e.contractors:
            if c.is_active and c.full_name.casefold() in message.casefold():
                matches.append((e.name, c.full_name))
    if not matches:
        return None, None
    if len({name for _emp, name in matches}) == 1 and len({emp for emp, _n in matches}) == 1:
        return matches[0]
    return None, matches[0][1]


def _benefit_request_args(ctx: chat_tools.ChatContext, message: str,
                          employer_ref: str | None) -> dict:
    """Pragmatically extract a benefit-request proposal from a message.

    Mirrors how the fallback parses run instructions: names are matched
    against org data, the amount/currency come from regexes, and the
    merchant is the phrase after "on"/"from". Anything missing stays
    ``None`` — the tool then asks a clarifying question instead of the
    fallback inventing values.
    """
    matched_employer, contractor_name = _contractor_ref_in(ctx, message)
    if contractor_name is None:
        m = _BENEFIT_WORKER_COLON_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    if contractor_name is None:
        m = _WORKER_FOR_AT_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    employer = employer_ref or matched_employer

    amount_match = _AMOUNT_RE.search(message)
    amount = amount_match.group(1).replace(",", "") if amount_match else None
    ccy_match = _BENEFIT_CCY_RE.search(message)
    currency = ccy_match.group(1).upper() if ccy_match else ("USD" if "$" in message else None)

    # The item lives after the colon ("... for Lena Fischer: a 149 USD
    # Python course on Coursera") or, without one, in the whole message
    # with the routing words and the worker's name removed.
    segment = message.split(":", 1)[1] if ":" in message else message
    merchant = None
    merchant_match = _BENEFIT_MERCHANT_RE.search(segment)
    if merchant_match:
        merchant = merchant_match.group(1).strip(" .!?")
        segment = segment[:merchant_match.start()]
    item_text = _BENEFIT_AMOUNT_STRIP_RE.sub("", segment, count=1)
    if contractor_name:
        item_text = re.sub(re.escape(contractor_name), "", item_text, flags=re.IGNORECASE)
    if ":" not in message:
        item_text = re.sub(
            r"\b(please|request|buy|order|get|book|purchase|benefit|benefits|for|a|an|the)\b",
            "", item_text, flags=re.IGNORECASE)
    item_text = " ".join(item_text.split()).strip(" ,.!?")
    # Drop a leading article left behind by removing the amount ("a Python course").
    while item_text.casefold().startswith(("a ", "an ", "the ")):
        item_text = item_text.split(" ", 1)[1]
    item = item_text or None
    return {"employer": employer, "contractor": contractor_name, "merchant": merchant,
            "item": item, "amount": amount, "currency": currency}


def _kit_args(ctx: chat_tools.ChatContext, message: str,
              employer_ref: str | None) -> dict:
    """Extract an onboarding-kit proposal, mirroring ``_benefit_request_args``.

    Names are matched against org data; a semicolon-separated item spec in
    the message is passed through verbatim for the tool to parse, and with
    no spec the tool uses the standard kit. Nothing is invented.
    """
    matched_employer, contractor_name = _contractor_ref_in(ctx, message)
    if contractor_name is None:
        m = _BENEFIT_WORKER_COLON_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    if contractor_name is None:
        m = _WORKER_FOR_AT_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    employer = employer_ref or matched_employer
    items = None
    if ";" in message and "$" in message:
        cleaned = message
        for name in (contractor_name, employer):
            if name:
                cleaned = re.sub(re.escape(name), "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"\b(please|propose|create|make|an?|onboarding|kit|for)\b", "",
            cleaned, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split()).strip(" ,")
        if ";" in cleaned and "$" in cleaned:
            items = cleaned
    return {"employer": employer, "contractor": contractor_name, "items": items}


def _amount_near(message: str, keywords: tuple[str, ...]) -> str | None:
    """A $ amount written next to one of ``keywords`` ("$450 flight", "hotel: $180")."""
    alt = "|".join(keywords)
    m = re.search(rf"(?:{alt})[^$0-9]{{0,20}}\$?\s*([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)",
                  message, re.IGNORECASE)
    if m:
        return m.group(1).replace(",", "")
    m = re.search(rf"\$\s*([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)[^$0-9]{{0,20}}(?:{alt})",
                  message, re.IGNORECASE)
    return m.group(1).replace(",", "") if m else None


def _trip_args(ctx: chat_tools.ChatContext, message: str,
               employer_ref: str | None) -> dict:
    """Extract a trip proposal: destination and per-category $ amounts."""
    matched_employer, contractor_name = _contractor_ref_in(ctx, message)
    if contractor_name is None:
        m = _BENEFIT_WORKER_COLON_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    if contractor_name is None:
        m = _WORKER_FOR_AT_RE.search(message)
        contractor_name = m.group(1).strip() if m else None
    employer = employer_ref or matched_employer
    destination = None
    dest_match = re.search(r"trip to ([A-Za-z ]+)", message, re.IGNORECASE) or \
        re.search(r"\bto\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)", message)
    if dest_match:
        destination = dest_match.group(1).strip(" .!?")
        for cut in ("for", "with", "on"):
            parts = re.split(rf"\s+{cut}\s+", destination, flags=re.IGNORECASE)
            if len(parts) > 1:
                destination = parts[0].strip(" .!?")
                break
        if contractor_name and destination.casefold() == contractor_name.casefold():
            destination = None
    return {"employer": employer, "contractor": contractor_name, "destination": destination,
            "transport": _amount_near(message, ("flight", "transport")),
            "lodging": _amount_near(message, ("hotel", "lodging")),
            "per_diem": _amount_near(message, ("per-diem", "per diem", "per_diem"))}


def fallback_plan(ctx: chat_tools.ChatContext, message: str,
                  context: dict) -> tuple[list[tuple[str, dict]], str, str | None]:
    """Plan one user message deterministically.

    Returns ``(steps, intent_detail, reply_prefix)``: the tool steps to
    execute (name + real arguments), a human-readable description of the
    routing decision for the ``intent_routing`` thinking event, and an
    optional prefix for the reply text (used only by the help fallback).
    Planning reads the employer's names from the DB; it executes nothing.
    """
    text = message.casefold()
    employer_ref = _employer_ref_in(ctx, message) or context.get("employer_name")
    run_ref = context.get("run_id")

    if "benefit" in text:
        _emp_match, worker_match = _contractor_ref_in(ctx, message)
        is_proposal = (
            any(w in text for w in ("request a benefit", "request benefit", "buy", "purchase", "order"))
            or (worker_match is not None and _AMOUNT_RE.search(message) is not None)
            or ("benefit" in text and " for " in text and ":" in message))
        if is_proposal:
            return ([("propose_benefit_purchase", _benefit_request_args(ctx, message, employer_ref))],
                    "Matched the propose-a-benefit intent (creates a REQUESTED request only — "
                    "a human approver decides in the Benefits screen; chat cannot approve or buy)", None)
        return ([("read_benefits", {"employer": employer_ref})],
                "Matched the Silo Benefits read intent (budgets, payroll lock and recent requests)", None)
    # VIC spend categories. Placed after benefits (benefit intents keep
    # their precedence) and before payroll routing; none of the keywords
    # below appear in payroll intents, so run routing is unaffected.
    if "pending" in text and "onboarding" in text:
        return ([("read_onboarding", {"employer": employer_ref})],
                "Matched the onboarding pending intent (draft kits awaiting approval and "
                "approved kits with unpurchased items)", None)
    if "pending" in text and ("trip" in text or "travel" in text):
        return ([("read_travel", {"employer": employer_ref})],
                "Matched the travel pending intent (trips awaiting approval and blocked "
                "components)", None)
    if "onboarding kit" in text or "kit for" in text:
        return ([("propose_onboarding_kit", _kit_args(ctx, message, employer_ref))],
                "Matched the propose-an-onboarding-kit intent (creates a DRAFT kit, source "
                "CHAT, only — a human approver decides in the Onboarding screen; chat cannot "
                "approve or buy)", None)
    if "onboarding" in text:
        return ([("read_onboarding", {"employer": employer_ref})],
                "Matched the onboarding read intent (budgets, kits and pending state)", None)
    if "trip" in text and any(w in text for w in ("propose", "plan", "book", "create")):
        return ([("propose_trip", _trip_args(ctx, message, employer_ref))],
                "Matched the propose-a-trip intent (creates a REQUESTED trip only — a human "
                "approver decides in the Travel screen; chat cannot approve, book or buy)", None)
    if "travel" in text or "trip" in text:
        return ([("read_travel", {"employer": employer_ref})],
                "Matched the travel read intent (budgets, category caps, trips and blocked components)", None)
    if "workflow" in text or "automation" in text:
        return ([("read_workflow_runs", {"employer": employer_ref})],
                "Matched the workflow-runs read intent (workflows are managed in the "
                "Workflows screen; chat only reads run status)", None)
    if any(w in text for w in ("timeline", "what happened", "history of", "events")):
        return ([("get_run_timeline", {"run_id": run_ref, "employer": employer_ref})],
                "Matched the run-timeline intent", None)
    if ("why" in text and ("held" in text or "short" in text)) or "explain" in text \
            or "shortfall" in text or ("held" in text and "?" in message) or "held" in text:
        return ([("explain_run", {"run_id": run_ref, "employer": employer_ref})],
                "Matched the explain-a-run intent (why a payroll is held or short)", None)
    if "approval" in text or "approve" in text:
        return ([("request_approval", {"run_id": run_ref, "employer": employer_ref})],
                "Matched the request-approval intent (surfacing a human decision, never making one)", None)
    if any(w in text for w in ("fund", "deposit", "top up", "top-up", "add money")):
        m = _AMOUNT_RE.search(message)
        amount = m.group(1).replace(",", "") if m else None
        return ([("demo_fund_employer", {"employer": employer_ref, "amount": amount})],
                "Matched the demo-funding intent for the employer's own wallet", None)
    if ("start" in text or "run payroll" in text or "new run" in text) and "run" in text:
        return ([("start_payroll_run", {"employer": employer_ref})],
                "Matched the start-a-payroll-run intent (the run will pause for human approval)", None)
    if "balance" in text or "how much" in text and "have" in text:
        return ([("get_employer_balance", {"employer": employer_ref})],
                "Matched the wallet-balance intent", None)
    if "run" in text and any(w in text for w in ("list", "show", "recent", "status")):
        return ([("list_runs", {"employer": employer_ref})],
                "Matched the list-runs intent", None)
    if "employer" in text or "client" in text:
        return ([("list_employers", {})], "Matched the list-employers intent", None)
    return ([("list_employers", {})],
            "No specific intent matched; answering with the capability summary and the employer list",
            _HELP_INTRO)


def fallback_turn(ctx: chat_tools.ChatContext, message: str,
                  context: dict) -> Iterator[tuple[str, chat_tools.ToolResult]]:
    """Yield (tool_name, result) steps for one user message, deterministically.

    Kept for callers that only need the executed results; the streaming
    service executes :func:`fallback_plan` step by step instead, so each
    ``tool_start`` event is emitted before its tool actually runs.
    """
    steps, _detail, prefix = fallback_plan(ctx, message, context)
    for name, kwargs in steps:
        result = _run_tool(ctx, name, kwargs)
        if prefix:
            result = chat_tools.ToolResult(prefix + result.text, approval=result.approval, data=result.data)
        yield name, result


# ---------------------------------------------------------------------------
# LLM turn (used only when a provider key is configured)
# ---------------------------------------------------------------------------

def _llm_turn(ctx: chat_tools.ChatContext, model, history: list[ChatMessage],
              message: str) -> Iterator[dict]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    lc_tools, wrappers = build_llm_tools(ctx)
    bound = model.bind_tools(lc_tools)
    messages = [SystemMessage(SYSTEM_PROMPT)]
    for h in history[-20:]:
        messages.append(HumanMessage(h.content) if h.role == "user" else AIMessage(h.content))
    messages.append(HumanMessage(message))

    reply_text = ""
    for round_no in range(6):  # bounded tool-calling loop
        yield _thinking("planning", "start",
                        "Asking the model to plan the next step" if round_no == 0
                        else f"Asking the model to continue (step {round_no + 1})")
        ai = bound.invoke(messages)
        messages.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        yield _thinking("planning", "done",
                        f"Model requested {len(calls)} tool call(s)" if calls
                        else "Model answered directly, without further tools")
        if not calls:
            reply_text = _reply_text(ai.content)
            break
        for call in calls:
            name = call["name"]
            args = call.get("args", {})
            yield _tool_start_event(name, args)
            if name in wrappers:
                result_text = wrappers[name](**(args or {}))
                result = wrappers[name].last
            else:  # the model named a tool that does not exist: tell it, don't crash the turn
                result = _run_tool(ctx, name, args)
                result_text = result.text
            yield _tool_result_event(name, result)
            if result and result.approval:
                yield {"type": "approval", **result.approval}
            messages.append(ToolMessage(content=result_text, tool_call_id=call["id"]))
    else:
        reply_text = "I could not complete that with the available tools."
    yield from _tokens(reply_text)
    return reply_text


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def stream_chat(db: Session, *, settings: Settings, runtime: PlatformRuntime,
                organization_id: uuid.UUID, organization_name: str, user_id: uuid.UUID,
                user_email: str, role: str, thread_id: uuid.UUID | None, message: str,
                context: dict | None = None) -> Iterator[dict]:
    """Run one chat turn, yielding SSE-shaped event dicts. Persists both sides."""
    context = context or {}
    if thread_id is not None:
        thread = get_thread(db, organization_id=organization_id, user_id=user_id, thread_id=thread_id)
        if thread is None:
            yield {"type": "error", "message": "Chat thread not found."}
            return
    else:
        thread = create_thread(db, organization_id=organization_id, user_id=user_id, first_message=message)

    # The ordered, honest record of this turn's work. Every event appended
    # here is yielded to the client at the moment it is recorded, and the
    # list is persisted on the assistant message when the turn completes.
    trace: list[dict] = []

    def emit(event: dict) -> dict:
        if event.get("type") in _TRACE_TYPES:
            trace.append(event)
        return event

    ctx = chat_tools.ChatContext(
        db=db, runtime=runtime, organization_id=organization_id, organization_name=organization_name,
        user_id=user_id, user_email=user_email, role=role, demo_mode=settings.demo_mode,
        page_employer_id=context.get("employer_id"), page_run_id=context.get("run_id"))

    yield emit(_thinking("context_load", "start", "Loading this thread's history and organization context"))
    # UI context ids are untrusted input: validate them against the org and drop fakes.
    dropped: list[str] = []
    for key, resolver in (("employer_id", chat_tools.resolve_employer), ("run_id", chat_tools.resolve_run)):
        raw = context.get(key)
        if raw and resolver(ctx, raw) is None:
            log_security_event(db, organization_id=organization_id, user_id=user_id, thread_id=thread.id,
                               kind="foreign_context_id", detail=f"context {key}={raw} is not in this organization")
            context = {**context, key: None}
            dropped.append(key)
    history = thread_messages(db, thread)
    detail = f"Loaded {len(history)} prior message(s) in this thread"
    if dropped:
        detail += f"; dropped foreign context id(s): {', '.join(dropped)}"
    yield emit(_thinking("context_load", "done", detail))
    _save(db, thread, "user", message)

    yield emit(_thinking("guard_check", "start",
                         "Screening the message for cross-tenant and instruction-override attempts"))
    refusal = screen_message(db, organization_id, message)
    if refusal is not None:
        kind, refusal_text = refusal
        yield emit(_thinking("guard_check", "done",
                             f"Refused ({kind}): the request crosses a silo or overrides a system rule; "
                             "no tools were run"))
        log_security_event(db, organization_id=organization_id, user_id=user_id, thread_id=thread.id,
                           kind=kind, detail=message)
        _save(db, thread, "assistant", refusal_text, trace=trace)
        yield from _tokens(refusal_text)
        yield {"type": "done", "thread_id": str(thread.id)}
        return
    yield emit(_thinking("guard_check", "done",
                         "No cross-tenant or instruction-override pattern detected"))

    reply_parts: list[str] = []
    try:
        model = get_chat_model(settings)
        if model is None:
            steps, intent_detail, prefix = fallback_plan(ctx, message, context)
            yield emit(_thinking("intent_routing", "start",
                                 "No LLM key configured; routing the message with the deterministic intent router"))
            yield emit(_thinking("intent_routing", "done", intent_detail))
            for name, kwargs in steps:
                yield emit(_tool_start_event(name, kwargs))
                result = _run_tool(ctx, name, kwargs)
                yield emit(_tool_result_event(name, result))
                if result.approval:
                    yield {"type": "approval", **result.approval}
                text = (prefix + result.text) if prefix else result.text
                reply_parts.append(text)
                yield from _tokens(text)
        else:
            yield emit(_thinking("intent_routing", "start",
                                 "Routing the message to the configured LLM with the org-scoped tools"))
            yield emit(_thinking("intent_routing", "done",
                                 "Live LLM engine selected; the model may only call the org-scoped tools"))
            collected: list[str] = []

            def _capture(gen):
                for event in gen:
                    if event.get("type") == "token":
                        collected.append(event["text"])
                    yield emit(event)

            yield from _capture(_llm_turn(ctx, model, history, message))
            reply_parts.append("".join(collected))
    except Exception as exc:  # a chat failure must surface as an error event, never a 500 mid-stream
        logger.exception("Silo chat turn failed")
        yield {"type": "error", "message": f"Chat failed: {exc}"}
        return
    _save(db, thread, "assistant", "\n".join(p for p in reply_parts if p), trace=trace)
    yield {"type": "done", "thread_id": str(thread.id)}
