"""Small administrative API for durable jobs."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ysparr.core.jobs import JobConflict, JobNotFound, JobManager


router = APIRouter(prefix="/ysparr/v1")


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
async def jobs(request: Request, limit: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    records = await _manager(request).store.list(limit)
    return {"object": "list", "data": [record.summary() for record in records]}


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str) -> dict[str, Any]:
    record = await _manager(request).store.get(job_id, include_events=True)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _detail(record)


@router.delete("/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    try:
        record = await _manager(request).cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record.summary()
