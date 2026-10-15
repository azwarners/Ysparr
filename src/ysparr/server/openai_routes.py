"""OpenAI-compatible client routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ysparr.apmatia import (
    ApmatiaAgentNotFound,
    ApmatiaUnavailable,
    named_model_id,
    parse_virtual_model_id,
    virtual_model_id,
)
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
        result = await _adapter(request).list_models()
    except UpstreamError as exc:
        return _error(exc)
    client = getattr(request.app.state, "apmatia_client", None)
    if client is not None:
        try:
            agents = await client.list_agents()
        except ApmatiaUnavailable:
            # Discovery is optional: retain upstream models and never reuse stale agents.
            agents = []
        existing = {model.get("id") for model in result.get("data", []) if isinstance(model, dict)}
        for agent in agents:
            model_id = named_model_id(agent["name"])
            if model_id not in existing:
                result["data"].append(
                    {"id": model_id, "object": "model", "owned_by": "apmatia"}
                )
    return JSONResponse(content=result)


@router.post("/chat/completions")
async def chat_completions(payload: ChatCompletionRequest, request: Request):
    upstream_request = payload.model_dump(exclude_none=True)
    client = getattr(request.app.state, "apmatia_client", None)
    agent = None
    if payload.model.startswith("apmatia-agent-"):
        if client is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"message": "Apmatia agent models are not enabled.", "type": "invalid_request_error"}},
            )
        try:
            agent_id = parse_virtual_model_id(payload.model)
            if agent_id is not None:
                agent = await client.get_agent(agent_id)
            else:
                raise ValueError("Invalid Apmatia agent model ID.")
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": {"message": str(exc), "type": "invalid_request_error"}})
        except ApmatiaAgentNotFound as exc:
            return JSONResponse(status_code=404, content={"error": {"message": str(exc), "type": "invalid_request_error"}})
        except ApmatiaUnavailable as exc:
            return JSONResponse(status_code=503, content={"error": {"message": str(exc), "type": "service_unavailable"}})
    elif client is not None:
        # Some clients use the model ID as the display label and ignore `name`.
        # Resolve a selected public agent name back to its current Apmatia record.
        try:
            match = next(
                (item for item in await client.list_agents() if named_model_id(item["name"]) == payload.model),
                None,
            )
            if match is not None:
                agent = await client.get_agent(match["id"])
        except ApmatiaUnavailable:
            # An ordinary upstream model can still work if Apmatia is unavailable.
            pass
    if agent is not None:
        model = agent.get("model")
        if not isinstance(model, dict) or not isinstance(model.get("model"), str) or not model["model"]:
            return JSONResponse(
                status_code=422,
                content={"error": {"message": "Published Apmatia agent has no available model mapping.", "type": "invalid_request_error"}},
            )
        # Put the compiled agent prompt first; preserve every client message and its order verbatim.
        upstream_request["model"] = model["model"]
        upstream_request["messages"] = [
            {"role": "system", "content": agent["system_prompt"]},
            *upstream_request["messages"],
        ]
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
