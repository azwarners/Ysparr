"""Storage contracts and durable job records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class JobRecord:
    id: str
    created_at: str
    updated_at: str
    state: str
    model: str
    request: dict[str, Any]
    response: dict[str, Any] | None
    response_content: str | None
    event_count: int
    delivered: bool
    orphaned: bool
    disconnected: bool
    failure: str | None
    failure_status_code: int | None
    events: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "delivered": self.delivered,
            "orphaned": self.orphaned,
            "disconnected": self.disconnected,
            "failure": self.failure,
            "response_available": self.response is not None or self.event_count > 0,
            "event_count": self.event_count,
        }


class JobStore(Protocol):
    async def create(self, job_id: str, request: dict[str, Any]) -> JobRecord: ...

    async def mark_running(self, job_id: str) -> None: ...

    async def append_event(self, job_id: str, event: bytes) -> None: ...

    async def complete(
        self,
        job_id: str,
        response: dict[str, Any] | None,
        response_content: str | None,
        orphaned: bool,
    ) -> None: ...

    async def fail(self, job_id: str, message: str, status_code: int = 502) -> None: ...

    async def mark_disconnected(self, job_id: str) -> None: ...

    async def mark_delivered(self, job_id: str) -> None: ...

    async def cancel(self, job_id: str) -> None: ...

    async def get(self, job_id: str, include_events: bool = False) -> JobRecord | None: ...

    async def list(self, limit: int = 50) -> list[JobRecord]: ...

    async def expire(self, completed: int, failed: int, orphaned: int) -> int: ...

    async def close(self) -> None: ...
