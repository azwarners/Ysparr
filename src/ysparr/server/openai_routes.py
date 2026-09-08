"""OpenAI-compatible client routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ysparr.core.jobs import JobManager
from ysparr.upstream.base import UpstreamAdapter, UpstreamError


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


router = APIRouter(prefix="/v1")


def _adapter(request: Request) -> UpstreamAdapter:
    return request.app.state.adapter


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def _error(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc), "type": "upstream_error"}},
    )


@router.get("/models")
async def models(request: Request) -> JSONResponse:
    try:
        return JSONResponse(content=await _adapter(request).list_models())
    except UpstreamError as exc:
        return _error(exc)


@router.post("/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, request: Request):
    upstream_request = payload.model_dump(exclude_none=True)
    if payload.stream:
        job_id = await _manager(request).submit(upstream_request)
        return StreamingResponse(_manager(request).stream(job_id), media_type="text/event-stream")
    job_id = await _manager(request).submit(upstream_request)
    try:
        record = await _manager(request).wait(job_id)
    except asyncio.CancelledError:
        await _manager(request).mark_disconnected(job_id)
        raise
    if record.state == "failed":
        error = UpstreamError(record.failure or "upstream execution failed", record.failure_status_code or 502)
        await _manager(request).release(job_id)
        return _error(error)
    if record.state == "cancelled":
        await _manager(request).release(job_id)
        return _error(UpstreamError("job was cancelled", 499))
    await _manager(request).mark_delivered(job_id)
    return JSONResponse(content=record.response or {})
