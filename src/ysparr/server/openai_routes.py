"""OpenAI-compatible client routes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ysparr.core.jobs import JobManager
from ysparr.extensions import (
    ExtensionClient,
    ExtensionEntryNotFound,
    ExtensionUnavailable,
    matching_entry,
)
from ysparr.security import Principal, principal_for_request, require_capability
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


def _extensions(request: Request) -> list[ExtensionClient]:
    return list(getattr(request.app.state, "extension_clients", []))


def _error(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc), "type": "upstream_error"}},
    )


@router.get("/models")
async def models(
    request: Request, principal: Principal | None = Depends(principal_for_request)
) -> JSONResponse:
    try:
        result = await _adapter(request).list_models()
    except UpstreamError as exc:
        return _error(exc)

    data = result.setdefault("data", [])
    existing = {model.get("id") for model in data if isinstance(model, dict)}
    for extension in _extensions(request):
        try:
            entries = await extension.catalog()
        except ExtensionUnavailable:
            # Discovery is optional. Never publish stale catalog data.
            continue
        for entry in entries:
            # Configuration order is the collision policy: the first owner wins.
            if entry["id"] in existing:
                continue
            data.append(
                {
                    "id": entry["id"],
                    "object": "model",
                    "owned_by": entry.get("owned_by", "extension"),
                }
            )
            existing.add(entry["id"])
    if principal is not None:
        result["data"] = [
            model
            for model in data
            if isinstance(model, dict) and principal.allows_model(str(model.get("id", "")))
        ]
    return JSONResponse(content=result)


async def _resolve_extension(
    request: Request,
    model: str,
    upstream_request: dict[str, Any],
) -> tuple[dict[str, Any] | None, bool, bool]:
    """Resolve one selector using configured extensions in operator order.

    Returns `(resolution, saw_catalog, saw_unavailable_catalog)`. Only the
    first matching owner receives the request, making catalog collisions
    deterministic and preventing unregistered extensions from inspecting it.
    """
    saw_catalog = False
    saw_unavailable_catalog = False
    for extension in _extensions(request):
        try:
            entries = await extension.catalog()
        except ExtensionUnavailable:
            saw_unavailable_catalog = True
            continue
        saw_catalog = True
        if matching_entry(entries, model) is None:
            continue
        try:
            return await extension.resolve(model, deepcopy(upstream_request)), saw_catalog, saw_unavailable_catalog
        except ExtensionEntryNotFound:
            return None, saw_catalog, saw_unavailable_catalog
    return None, saw_catalog, saw_unavailable_catalog


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    principal: Principal | None = Depends(principal_for_request),
):
    require_capability(principal, "chat")
    if principal is not None and not principal.allows_model(payload.model):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": "model is not permitted for this key",
                    "type": "permission_error",
                }
            },
        )

    upstream_request = payload.model_dump(exclude_none=True)
    resolution, saw_catalog, saw_unavailable_catalog = await _resolve_extension(
        request, payload.model, upstream_request
    )
    if resolution is not None:
        upstream_request["messages"] = [
            *resolution["contributions"],
            *upstream_request["messages"],
        ]
        upstream_request["_ysparr_target"] = resolution["target"]
    elif payload.model.startswith("apmatia-agent-"):
        if saw_unavailable_catalog and not saw_catalog:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "the configured agent extension is unavailable",
                        "type": "service_unavailable",
                    }
                },
            )
        if _extensions(request):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": "extension entry not found",
                        "type": "invalid_request_error",
                    }
                },
            )
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "agent extensions are not enabled",
                    "type": "invalid_request_error",
                }
            },
        )

    if payload.stream:
        job_id = await _manager(request).submit(
            upstream_request,
            openwebui_chat_id=request.headers.get("X-OpenWebUI-Chat-Id") or None,
            principal_id=principal.id if principal else None,
        )
        return StreamingResponse(_manager(request).stream(job_id), media_type="text/event-stream")

    job_id = await _manager(request).submit(
        upstream_request,
        openwebui_chat_id=request.headers.get("X-OpenWebUI-Chat-Id") or None,
        principal_id=principal.id if principal else None,
    )
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
