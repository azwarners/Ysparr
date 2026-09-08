"""In-process durable job execution and downstream subscription management."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ysparr.persistence.store import JobRecord, JobStore
from ysparr.upstream.base import UpstreamAdapter, UpstreamError


class JobNotFound(LookupError):
    """Requested job does not exist."""


class JobConflict(RuntimeError):
    """Requested job operation is not valid for its current state."""


_END = object()
logger = logging.getLogger("ysparr.jobs")


@dataclass
class _Runtime:
    queue: asyncio.Queue[bytes | object] = field(default_factory=asyncio.Queue)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    disconnected: bool = False
    sse_buffer: str = ""
    content: list[str] = field(default_factory=list)


def _response_content(response: dict[str, Any]) -> str | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


def _sse_content(runtime: _Runtime, chunk: bytes) -> None:
    runtime.sse_buffer += chunk.decode("utf-8", errors="replace")
    while "\n\n" in runtime.sse_buffer:
        event, runtime.sse_buffer = runtime.sse_buffer.split("\n\n", 1)
        for line in event.splitlines():
            if not line.startswith("data: ") or line[6:] == "[DONE]":
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                runtime.content.append(content)


class JobManager:
    """Own upstream tasks independently from individual HTTP request tasks."""

    def __init__(
        self,
        store: JobStore,
        adapter: UpstreamAdapter,
        completed_retention_seconds: int = 7 * 86400,
        failed_retention_seconds: int = 86400,
        orphaned_retention_seconds: int = 30 * 86400,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.retention = (
            completed_retention_seconds,
            failed_retention_seconds,
            orphaned_retention_seconds,
        )
        self._runtimes: dict[str, _Runtime] = {}

    async def submit(self, request: dict[str, Any]) -> str:
        expired = await self.store.expire(*self.retention)
        if expired:
            logger.info("jobs expired count=%s", expired)
        job_id = str(uuid.uuid4())
        await self.store.create(job_id, request)
        runtime = _Runtime()
        self._runtimes[job_id] = runtime
        runtime.task = asyncio.create_task(self._execute(job_id, request, runtime))
        logger.info("job accepted id=%s model=%s", job_id, request["model"])
        return job_id

    async def _execute(self, job_id: str, request: dict[str, Any], runtime: _Runtime) -> None:
        try:
            await self.store.mark_running(job_id)
            logger.info("job running id=%s", job_id)
            if request.get("stream") is True:
                await self._execute_stream(job_id, request, runtime)
            else:
                response = await self.adapter.complete(request)
                await self.store.complete(
                    job_id,
                    response,
                    _response_content(response),
                    runtime.disconnected,
                )
                logger.info("job completed id=%s", job_id)
        except asyncio.CancelledError:
            record = await self.store.get(job_id)
            if record is not None and record.state != "cancelled":
                await self.store.fail(job_id, "interrupted by task cancellation", 503)
            raise
        except UpstreamError as exc:
            await self.store.fail(job_id, str(exc), exc.status_code)
            logger.warning("job failed id=%s reason=%s", job_id, str(exc)[:200])
            if not runtime.disconnected and request.get("stream") is True:
                error = {"error": {"message": str(exc), "type": "upstream_error"}}
                await runtime.queue.put(f"data: {json.dumps(error, separators=(',', ':'))}\n\n".encode())
                await runtime.queue.put(_END)
        except Exception as exc:  # Keep task failures visible in the durable record.
            await self.store.fail(job_id, str(exc), 502)
            logger.exception("job failed id=%s", job_id)
            if not runtime.disconnected and request.get("stream") is True:
                error = {"error": {"message": "upstream execution failed", "type": "upstream_error"}}
                await runtime.queue.put(f"data: {json.dumps(error, separators=(',', ':'))}\n\n".encode())
                await runtime.queue.put(_END)
        finally:
            runtime.done.set()
            if runtime.disconnected:
                self._runtimes.pop(job_id, None)

    async def _execute_stream(self, job_id: str, request: dict[str, Any], runtime: _Runtime) -> None:
        async for chunk in self.adapter.stream(request):
            await self.store.append_event(job_id, chunk)
            _sse_content(runtime, chunk)
            if not runtime.disconnected:
                await runtime.queue.put(chunk)
        await self.store.complete(
            job_id,
            None,
            "".join(runtime.content) or None,
            runtime.disconnected,
        )
        logger.info("job %s id=%s", "orphaned" if runtime.disconnected else "completed", job_id)
        if not runtime.disconnected:
            await runtime.queue.put(_END)

    async def wait(self, job_id: str) -> JobRecord:
        runtime = self._runtimes.get(job_id)
        if runtime is None:
            record = await self.store.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            return record
        await asyncio.shield(runtime.done.wait())
        record = await self.store.get(job_id)
        if record is None:
            raise JobNotFound(job_id)
        return record

    async def mark_disconnected(self, job_id: str) -> None:
        runtime = self._runtimes.get(job_id)
        if runtime is not None:
            runtime.disconnected = True
        logger.info("client disconnected id=%s", job_id)
        await self.store.mark_disconnected(job_id)

    async def mark_delivered(self, job_id: str) -> JobRecord:
        await self.store.mark_delivered(job_id)
        record = await self.store.get(job_id)
        if record is None:
            raise JobNotFound(job_id)
        self._runtimes.pop(job_id, None)
        return record

    async def release(self, job_id: str) -> None:
        """Drop in-memory delivery state after a terminal response was handled."""

        self._runtimes.pop(job_id, None)

    async def stream(self, job_id: str) -> AsyncIterator[bytes]:
        runtime = self._runtimes.get(job_id)
        if runtime is None:
            raise JobNotFound(job_id)
        delivered = False
        try:
            while True:
                item = await runtime.queue.get()
                if item is _END:
                    await self.mark_delivered(job_id)
                    delivered = True
                    return
                yield item
        finally:
            if not delivered:
                await self.mark_disconnected(job_id)

    async def cancel(self, job_id: str) -> JobRecord:
        record = await self.store.get(job_id)
        if record is None:
            raise JobNotFound(job_id)
        if record.state not in {"accepted", "queued", "running"}:
            raise JobConflict(f"job is already {record.state}")
        await self.store.cancel(job_id)
        logger.info("job cancelled id=%s", job_id)
        runtime = self._runtimes.get(job_id)
        if runtime is not None and runtime.task is not None:
            runtime.task.cancel()
            await asyncio.gather(runtime.task, return_exceptions=True)
        result = await self.store.get(job_id)
        if result is None:
            raise JobNotFound(job_id)
        return result

    async def shutdown(self) -> None:
        tasks = [runtime.task for runtime in self._runtimes.values() if runtime.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()
