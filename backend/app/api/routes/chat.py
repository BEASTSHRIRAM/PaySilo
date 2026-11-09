"""Silo chat routes: SSE streaming chat plus per-user thread history.

Contract (fixed — the frontend builds against this):

- ``POST /chat`` — body ``{thread_id?, message, context?}``; response is an
  SSE stream of ``token`` / ``tool`` / ``approval`` / ``done`` / ``error``
  events.
- ``GET /chat/threads`` — the signed-in user's threads.
- ``GET /chat/threads/{thread_id}/messages`` — messages of the owner's
  thread only; another user's or organization's thread is a 404.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...chat import service
from ..deps import Tenant, current_tenant, get_db, get_runtime, get_settings

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatContextIn(BaseModel):
    employer_id: str | None = None
    run_id: str | None = None
    page: str | None = None


class ChatIn(BaseModel):
    thread_id: str | None = None
    message: str = Field(min_length=1, max_length=20000)
    context: ChatContextIn | None = None


class ThreadOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    role: str
    content: str
    created_at: datetime


def _sse(event: dict) -> str:
    kind = event.pop("type")
    return f"event: {kind}\ndata: {json.dumps(event)}\n\n"


@router.post("")
def chat(body: ChatIn, request: Request, tenant: Tenant = Depends(current_tenant)):
    settings = get_settings(request)
    runtime = get_runtime(request)
    thread_uuid: uuid.UUID | None = None
    if body.thread_id:
        try:
            thread_uuid = uuid.UUID(body.thread_id)
        except ValueError:
            thread_uuid = None
            bad_thread = True
        else:
            bad_thread = False
    else:
        bad_thread = False

    def generate():
        if bad_thread:
            yield _sse({"type": "error", "message": "Invalid thread_id."})
            return
        # Own session for the stream: it must outlive the request handler.
        with request.app.state.db.sessionmaker() as db:
            for event in service.stream_chat(
                    db, settings=settings, runtime=runtime,
                    organization_id=tenant.organization.id, organization_name=tenant.organization.name,
                    user_id=tenant.user.id, user_email=tenant.user.email, role=tenant.role,
                    thread_id=thread_uuid, message=body.message,
                    context=body.context.model_dump() if body.context else None):
                yield _sse(dict(event))

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/threads", response_model=list[ThreadOut])
def list_threads(tenant: Tenant = Depends(current_tenant), db: Session = Depends(get_db)) -> list[ThreadOut]:
    return [ThreadOut(id=str(t.id), title=t.title, created_at=t.created_at, updated_at=t.updated_at)
            for t in service.list_threads(db, organization_id=tenant.organization.id, user_id=tenant.user.id)]


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
def thread_messages(thread_id: uuid.UUID, tenant: Tenant = Depends(current_tenant),
                    db: Session = Depends(get_db)) -> list[MessageOut]:
    thread = service.get_thread(db, organization_id=tenant.organization.id,
                                user_id=tenant.user.id, thread_id=thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat thread not found.")
    return [MessageOut(role=m.role, content=m.content, created_at=m.created_at)
            for m in service.thread_messages(db, thread)]
