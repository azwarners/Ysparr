"""OpenAI-compatible client routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ysparr.upstream.base import UpstreamAdapter, UpstreamError


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


router = APIRouter(prefix="/v1")


def _adapter(request: Request) -> UpstreamAdapter:
    return request.app.state.adapter


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
    adapter = _adapter(request)
    if payload.stream:
        async def events() -> AsyncIterator[str]:
            try:
                async for chunk in adapter.stream(upstream_request):
                    yield chunk
            except UpstreamError as exc:
                error = {"error": {"message": str(exc), "type": "upstream_error"}}
                yield f"data: {json.dumps(error, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")
    try:
        return JSONResponse(content=await adapter.complete(upstream_request))
    except UpstreamError as exc:
        return _error(exc)
