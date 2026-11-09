"""Silo chat service: one streamed turn of conversation.

A turn is a generator of events in the fixed SSE contract:

    {"type": "token",    "text": ...}
    {"type": "tool",     "name": ..., "label": ..., "status": "start"|"done"}
    {"type": "approval", "run_id": ..., "employer_id": ..., "employer_name": ..., "summary": ...}
    {"type": "done",     "thread_id": ...}
    {"type": "error",    "message": ...}

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

import logging
import re
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
    "Answer using the tools provided; every tool is already scoped to the user's organization, "
    "and you must never ask for or invent another organization's data. Amounts and statuses must "
    "come from tool results, never from memory. You cannot approve a payroll run or move money: "
    "approvals are TAP-signed human decisions on the run itself. If a run is waiting, use "
    "request_approval to surface it. Keep answers concise and in plain English."
)

_AMOUNT_RE = re.compile(r"(?:USD\s*)?\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE)


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


def _save(db: Session, thread: ChatThread, role: str, content: str) -> None:
    db.add(ChatMessage(thread_id=thread.id, role=role, content=content))
    thread.title = thread.title  # touch: updates updated_at via onupdate
    db.commit()


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def _tokens(text: str) -> Iterator[dict]:
    """Stream a reply in small word-group tokens."""
    words = text.split(" ")
    for i in range(0, len(words), 6):
        chunk = " ".join(words[i:i + 6])
        yield {"type": "token", "text": chunk + (" " if i + 6 < len(words) else "")}


def _tool_events(name: str, result: chat_tools.ToolResult) -> Iterator[dict]:
    yield {"type": "tool", "name": name, "label": chat_tools.TOOL_LABELS.get(name, name), "status": "start"}
    yield {"type": "tool", "name": name, "label": chat_tools.TOOL_LABELS.get(name, name), "status": "done"}
    if result.approval:
        yield {"type": "approval", **result.approval}


def _run_tool(ctx: chat_tools.ChatContext, name: str, kwargs: dict) -> chat_tools.ToolResult:
    fn = chat_tools.TOOL_FUNCTIONS[name]
    allowed = {k: v for k, v in kwargs.items() if k in fn.__code__.co_varnames}
    return fn(ctx, **allowed)


# ---------------------------------------------------------------------------
# Deterministic fallback responder (no LLM key configured)
# ---------------------------------------------------------------------------

def _employer_ref_in(ctx: chat_tools.ChatContext, message: str) -> str | None:
    for e in ctx.db.scalars(select(Employer).where(Employer.organization_id == ctx.organization_id)):
        if e.name.casefold() in message.casefold() or e.name.split()[0].casefold() in message.casefold():
            return e.name
    return None


def fallback_turn(ctx: chat_tools.ChatContext, message: str,
                  context: dict) -> Iterator[tuple[str, chat_tools.ToolResult]]:
    """Yield (tool_name, result) steps for one user message, deterministically."""
    text = message.casefold()
    employer_ref = _employer_ref_in(ctx, message) or context.get("employer_name")
    run_ref = context.get("run_id")

    if any(w in text for w in ("timeline", "what happened", "history of", "events")):
        yield "get_run_timeline", chat_tools.get_run_timeline(ctx, run_id=run_ref, employer=employer_ref)
        return
    if ("why" in text and ("held" in text or "short" in text)) or "explain" in text \
            or "shortfall" in text or ("held" in text and "?" in message) or "held" in text:
        yield "explain_run", chat_tools.explain_run(ctx, run_id=run_ref, employer=employer_ref)
        return
    if "approval" in text or "approve" in text:
        yield "request_approval", chat_tools.request_approval(ctx, run_id=run_ref, employer=employer_ref)
        return
    if any(w in text for w in ("fund", "deposit", "top up", "top-up", "add money")):
        m = _AMOUNT_RE.search(message)
        yield "demo_fund_employer", chat_tools.demo_fund_employer(
            ctx, employer=employer_ref, amount=m.group(1).replace(",", "") if m else None)
        return
    if ("start" in text or "run payroll" in text or "new run" in text) and "run" in text:
        yield "start_payroll_run", chat_tools.start_payroll_run(ctx, employer=employer_ref)
        return
    if "balance" in text or "how much" in text and "have" in text:
        yield "get_employer_balance", chat_tools.get_employer_balance(ctx, employer=employer_ref)
        return
    if "run" in text and any(w in text for w in ("list", "show", "recent", "status")):
        yield "list_runs", chat_tools.list_runs(ctx, employer=employer_ref)
        return
    if "employer" in text or "client" in text:
        yield "list_employers", chat_tools.list_employers(ctx)
        return
    yield "list_employers", chat_tools.ToolResult(
        "I am Silo, your payroll assistant. I can list employers and balances, explain any run "
        "(including exactly why a payroll is held), show a run's timeline, start a run — which "
        "always pauses for a TAP-signed human approval — and, in demo mode, fund an employer's "
        "own wallet. I cannot approve a run or move money between employers. "
        + chat_tools.list_employers(ctx).text)


# ---------------------------------------------------------------------------
# LLM turn (used only when a provider key is configured)
# ---------------------------------------------------------------------------

def _llm_turn(ctx: chat_tools.ChatContext, model, history: list[ChatMessage],
              message: str) -> Iterator[dict]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
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
            func=call, name=name, description=f"{chat_tools.TOOL_LABELS[name]}. Scoped to the signed-in organization."))
    bound = model.bind_tools(lc_tools)
    messages = [SystemMessage(SYSTEM_PROMPT)]
    for h in history[-20:]:
        messages.append(HumanMessage(h.content) if h.role == "user" else AIMessage(h.content))
    messages.append(HumanMessage(message))

    reply_text = ""
    for _ in range(6):  # bounded tool-calling loop
        ai = bound.invoke(messages)
        messages.append(ai)
        if not getattr(ai, "tool_calls", None):
            reply_text = ai.content if isinstance(ai.content, str) else str(ai.content)
            break
        for call in ai.tool_calls:
            name = call["name"]
            yield {"type": "tool", "name": name,
                   "label": chat_tools.TOOL_LABELS.get(name, name), "status": "start"}
            result_text = wrappers[name](**call.get("args", {}))
            yield {"type": "tool", "name": name,
                   "label": chat_tools.TOOL_LABELS.get(name, name), "status": "done"}
            result = wrappers[name].last
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

    ctx = chat_tools.ChatContext(
        db=db, runtime=runtime, organization_id=organization_id, organization_name=organization_name,
        user_id=user_id, user_email=user_email, role=role, demo_mode=settings.demo_mode,
        page_employer_id=context.get("employer_id"), page_run_id=context.get("run_id"))
    # UI context ids are untrusted input: validate them against the org and drop fakes.
    for key, resolver in (("employer_id", chat_tools.resolve_employer), ("run_id", chat_tools.resolve_run)):
        raw = context.get(key)
        if raw and resolver(ctx, raw) is None:
            log_security_event(db, organization_id=organization_id, user_id=user_id, thread_id=thread.id,
                               kind="foreign_context_id", detail=f"context {key}={raw} is not in this organization")
            context = {**context, key: None}

    history = thread_messages(db, thread)
    _save(db, thread, "user", message)

    refusal = screen_message(db, organization_id, message)
    if refusal is not None:
        kind, refusal_text = refusal
        log_security_event(db, organization_id=organization_id, user_id=user_id, thread_id=thread.id,
                           kind=kind, detail=message)
        _save(db, thread, "assistant", refusal_text)
        yield from _tokens(refusal_text)
        yield {"type": "done", "thread_id": str(thread.id)}
        return

    reply_parts: list[str] = []
    try:
        model = get_chat_model(settings)
        if model is None:
            for name, result in fallback_turn(ctx, message, context):
                yield from _tool_events(name, result)
                reply_parts.append(result.text)
                yield from _tokens(result.text)
        else:
            collected: list[str] = []

            def _capture(gen):
                for event in gen:
                    if event.get("type") == "token":
                        collected.append(event["text"])
                    yield event

            yield from _capture(_llm_turn(ctx, model, history, message))
            reply_parts.append("".join(collected))
    except Exception as exc:  # a chat failure must surface as an error event, never a 500 mid-stream
        logger.exception("Silo chat turn failed")
        yield {"type": "error", "message": f"Chat failed: {exc}"}
        return
    _save(db, thread, "assistant", "\n".join(p for p in reply_parts if p))
    yield {"type": "done", "thread_id": str(thread.id)}
