"""OpenAI-compatible client routes."""

from __future__ import annotations

import asyncio
import re
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
    matching_context_entry,
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


def _principal_id(principal: Principal | None) -> str | None:
    return principal.id if principal is not None and not principal.public_access else None


def _is_reserved_apmatia_selector(model: str) -> bool:
    return re.fullmatch(r"apmatia-agent-[0-9]+", model) is not None


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
            entries = await extension.catalog(_principal_id(principal))
        except ExtensionUnavailable:
            # Discovery is optional. Never publish stale catalog data.
            continue
        for entry in entries:
            if entry.get("applies_to", "selector") != "selector":
                continue
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
    principal: Principal | None,
) -> tuple[dict[str, Any] | None, bool, bool, bool]:
    """Resolve one selector using configured extensions in operator order.

    Returns `(resolution, saw_catalog, saw_unavailable_catalog, matched)`. Only the
    first matching owner receives the request, making catalog collisions
    deterministic and preventing unregistered extensions from inspecting it.
    """
    saw_catalog = False
    saw_unavailable_catalog = False
    matched = False
    context_contributions: list[dict[str, Any]] = []
    selected_resolution: dict[str, Any] | None = None
    selected_extension_index: int | None = None
    private_agent_id: int | None = None
    for extension_index, extension in enumerate(_extensions(request)):
        # Once a selector owner is found, later registrations must not even
        # receive a catalog request for it. This makes collision ownership a
        # real trust boundary, while ordinary models can still collect context
        # from every configured context extension.
        if matched:
            break
        try:
            entries = await extension.catalog(_principal_id(principal))
        except ExtensionUnavailable:
            saw_unavailable_catalog = True
            continue
        saw_catalog = True
        context_entry = matching_context_entry(entries)
        if context_entry is not None:
            try:
                context_resolution = await extension.resolve(
                    model, deepcopy(upstream_request), _principal_id(principal)
                )
                if any(item["role"] != "developer" for item in context_resolution["contributions"]):
                    raise ExtensionUnavailable("context extensions may only contribute developer messages")
                context_contributions.extend(context_resolution["contributions"])
            except ExtensionUnavailable:
                # Context is optional by default. A failed context provider
                # must not make an otherwise valid ordinary model unusable.
                if extension.context_failure == "fail":
                    raise
                pass
        extension_entry = matching_entry(entries, model)
        if not matched and extension_entry is not None:
            # Discovery gives ordinary upstream IDs precedence over extension
            # IDs. Check that again only after an extension match so ordinary
            # requests preserve their existing single-dispatch behavior.
            try:
                upstream_models = await _adapter(request).list_models()
            except UpstreamError as exc:
                raise ExtensionUnavailable("could not validate the extension selector") from exc
            upstream_model_ids = {
                str(upstream_model.get("id"))
                for upstream_model in upstream_models.get("data", [])
                if isinstance(upstream_model, dict) and upstream_model.get("id") is not None
            }
            if model in upstream_model_ids:
                continue
            matched = True
            selected_extension_index = extension_index
            if extension_entry.get("id") == model:
                private_match = re.fullmatch(r"apmatia-agent-(\d+)", model)
                if private_match:
                    private_agent_id = int(private_match.group(1))
            try:
                selected_resolution = await extension.resolve(
                    model, deepcopy(upstream_request), _principal_id(principal)
                )
            except ExtensionEntryNotFound:
                # The catalog was authoritative when it matched. Do not silently
                # reinterpret a disappearing virtual model as an upstream model.
                return None, saw_catalog, saw_unavailable_catalog, matched
            except ExtensionUnavailable:
                # The selected extension is part of the request's route. A
                # transient failure must be explicit; implicit substitution would
                # change the selected application's behavior and trust boundary.
                raise
    if selected_resolution is None and not context_contributions:
        return None, saw_catalog, saw_unavailable_catalog, matched
    return {
        "contributions": [
            *context_contributions,
            *(selected_resolution or {}).get("contributions", []),
        ],
        "target": (selected_resolution or {}).get("target", {}),
        "extension_index": selected_extension_index,
        "private_agent_id": private_agent_id,
    }, saw_catalog, saw_unavailable_catalog, matched


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
    try:
        resolution, saw_catalog, saw_unavailable_catalog, matched = await _resolve_extension(
            request, payload.model, upstream_request, principal
        )
    except ExtensionUnavailable:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "the selected extension is unavailable",
                    "type": "service_unavailable",
                }
            },
        )
    if matched and resolution is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "extension entry is no longer available",
                    "type": "invalid_request_error",
                }
            },
        )
    if not matched and _is_reserved_apmatia_selector(payload.model):
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": "extension entry is not available",
                    "type": "invalid_request_error",
                }
            },
        )
    if resolution is not None:
        upstream_request["messages"] = [
            *resolution["contributions"],
            *upstream_request["messages"],
        ]
        if resolution["target"]:
            upstream_request["_ysparr_target"] = resolution["target"]
        private_agent_id = resolution.get("private_agent_id")
        if private_agent_id is not None and resolution.get("extension_index") is not None:
            try:
                tools = await _extensions(request)[resolution["extension_index"]].tools(
                    private_agent_id, _principal_id(principal)
                )
            except ExtensionUnavailable:
                return JSONResponse(
                    status_code=503,
                    content={"error": {"message": "authorized tool endpoint is unavailable", "type": "service_unavailable"}},
                )
            upstream_request["_ysparr_tool_context"] = {
                "extension_index": resolution["extension_index"],
                "agent_id": private_agent_id,
                "principal_id": _principal_id(principal),
                "tools": tools,
            }
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
