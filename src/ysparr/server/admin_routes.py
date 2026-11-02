"""Small administrative API for durable jobs."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ysparr.core.jobs import JobConflict, JobNotFound, JobManager
from ysparr.security import Principal, principal_for_request, require_capability


router = APIRouter(prefix="/ysparr/v1")


class OpenWebUIStopRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=512)


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def _detail(record: Any) -> dict[str, Any]:
    result = record.summary()
    result.update(
        {
            "request": record.request,
            "response": record.response,
            "response_content": record.response_content,
            "events": [base64.b64decode(event).decode("utf-8", errors="replace") for event in record.events],
        }
    )
    return result


@router.get("/jobs")
async def jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal | None = Depends(principal_for_request),
) -> dict[str, Any]:
    require_capability(principal, "jobs:read")
    records = await _manager(request).store.list(limit)
    if principal is not None and not principal.admin:
        records = [record for record in records if record.principal_id == principal.id]
    return {"object": "list", "data": [record.summary() for record in records]}


@router.get("/jobs/{job_id}")
async def job(
    request: Request, job_id: str, principal: Principal | None = Depends(principal_for_request)
) -> dict[str, Any]:
    require_capability(principal, "jobs:read")
    record = await _manager(request).store.get(job_id, include_events=True)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    if principal is not None and not principal.admin and record.principal_id != principal.id:
        raise HTTPException(status_code=404, detail="job not found")
    return _detail(record)


@router.delete("/jobs/{job_id}")
async def cancel_job(
    request: Request, job_id: str, principal: Principal | None = Depends(principal_for_request)
) -> dict[str, Any]:
    require_capability(principal, "jobs:cancel")
    record = await _manager(request).store.get(job_id)
    if principal is not None and (not record or (not principal.admin and record.principal_id != principal.id)):
        raise HTTPException(status_code=404, detail="job not found")
    try:
        record = await _manager(request).cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.summary()


@router.post("/openwebui/cancel")
async def cancel_openwebui_chat(
    request: Request,
    form: OpenWebUIStopRequest,
    principal: Principal | None = Depends(principal_for_request),
) -> dict[str, Any]:
    require_capability(principal, "jobs:cancel")
    if principal is not None and not principal.admin:
        raise HTTPException(status_code=403, detail="only administrator keys may cancel Open WebUI chats")
    job_ids = await _manager(request).cancel_openwebui_chat(form.chat_id)
    return {"status": True, "cancelled": job_ids}
