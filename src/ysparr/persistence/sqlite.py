"""Small SQLite-backed implementation of the Ysparr job store."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from ysparr.persistence.store import JobRecord

T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(row: sqlite3.Row, events: tuple[str, ...] = ()) -> JobRecord:
    response = json.loads(row["response_json"]) if row["response_json"] else None
    return JobRecord(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state=row["state"],
        model=row["model"],
        request=json.loads(row["request_json"]),
        response=response,
        response_content=row["response_content"],
        event_count=row["event_count"],
        delivered=bool(row["delivered"]),
        orphaned=bool(row["orphaned"]),
        disconnected=bool(row["disconnected"]),
        failure=row["failure"],
        failure_status_code=row["failure_status_code"],
        events=events,
    )


class SQLiteJobStore:
    """Thread-safe SQLite store with async methods for the service layer."""

    def __init__(self, path: str) -> None:
        self.path = os.path.expanduser(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                INSERT INTO schema_version(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    response_content TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    orphaned INTEGER NOT NULL DEFAULT 0,
                    disconnected INTEGER NOT NULL DEFAULT 0,
                    failure TEXT,
                    failure_status_code INTEGER,
                    event_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_updated_idx ON jobs(updated_at);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY(job_id, sequence)
                );
                """
            )
            self._connection.execute(
                """
                UPDATE jobs
                   SET state = 'failed',
                       failure = 'interrupted by Ysparr restart',
                       failure_status_code = 503,
                       updated_at = ?
                 WHERE state IN ('accepted', 'queued', 'running')
                """,
                (_now(),),
            )

    def _call(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            return function(*args, **kwargs)

    async def _run(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(self._call, function, *args, **kwargs)

    def _create(self, job_id: str, request: dict[str, Any]) -> JobRecord:
        timestamp = _now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs(id, created_at, updated_at, state, model, request_json)
                VALUES (?, ?, ?, 'accepted', ?, ?)
                """,
                (job_id, timestamp, timestamp, request["model"], json.dumps(request)),
            )
        return self._get(job_id)

    async def create(self, job_id: str, request: dict[str, Any]) -> JobRecord:
        return await self._run(self._create, job_id, request)

    def _simple_update(self, job_id: str, state: str, **values: Any) -> None:
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[Any] = [state, _now()]
        for key, value in values.items():
            assignments.append(f"{key} = ?")
            parameters.append(value)
        parameters.append(job_id)
        with self._connection:
            self._connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", parameters
            )

    async def mark_running(self, job_id: str) -> None:
        await self._run(self._simple_update, job_id, "running")

    def _append_event(self, job_id: str, event: bytes) -> None:
        with self._connection:
            row = self._connection.execute(
                "SELECT event_count FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            sequence = row["event_count"]
            self._connection.execute(
                "INSERT INTO job_events(job_id, sequence, payload) VALUES (?, ?, ?)",
                (job_id, sequence, event),
            )
            self._connection.execute(
                "UPDATE jobs SET event_count = ?, updated_at = ? WHERE id = ?",
                (sequence + 1, _now(), job_id),
            )

    async def append_event(self, job_id: str, event: bytes) -> None:
        await self._run(self._append_event, job_id, event)

    def _complete(
        self,
        job_id: str,
        response: dict[str, Any] | None,
        response_content: str | None,
        orphaned: bool,
    ) -> None:
        self._simple_update(
            job_id,
            "orphaned" if orphaned else "completed",
            response_json=json.dumps(response) if response is not None else None,
            response_content=response_content,
            orphaned=int(orphaned),
            disconnected=int(orphaned),
        )

    async def complete(
        self,
        job_id: str,
        response: dict[str, Any] | None,
        response_content: str | None,
        orphaned: bool,
    ) -> None:
        await self._run(self._complete, job_id, response, response_content, orphaned)

    async def fail(self, job_id: str, message: str, status_code: int = 502) -> None:
        await self._run(
            self._simple_update,
            job_id,
            "failed",
            failure=message[:1000],
            failure_status_code=status_code,
        )

    def _mark_disconnected(self, job_id: str) -> None:
        with self._connection:
            row = self._connection.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            state = "orphaned" if row["state"] == "completed" else row["state"]
            self._connection.execute(
                "UPDATE jobs SET state = ?, disconnected = 1, orphaned = ?, updated_at = ? WHERE id = ?",
                (state, int(state == "orphaned"), _now(), job_id),
            )

    async def mark_disconnected(self, job_id: str) -> None:
        await self._run(self._mark_disconnected, job_id)

    def _mark_delivered(self, job_id: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE jobs
                   SET state = 'delivered', delivered = 1, updated_at = ?
                 WHERE id = ? AND state = 'completed' AND disconnected = 0
                """,
                (_now(), job_id),
            )

    async def mark_delivered(self, job_id: str) -> None:
        await self._run(self._mark_delivered, job_id)

    async def cancel(self, job_id: str) -> None:
        await self._run(self._simple_update, job_id, "cancelled")

    def _get(self, job_id: str, include_events: bool = False) -> JobRecord | None:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        events: tuple[str, ...] = ()
        if include_events:
            payloads = self._connection.execute(
                "SELECT payload FROM job_events WHERE job_id = ? ORDER BY sequence", (job_id,)
            ).fetchall()
            events = tuple(base64.b64encode(payload[0]).decode("ascii") for payload in payloads)
        return _record(row, events)

    async def get(self, job_id: str, include_events: bool = False) -> JobRecord | None:
        return await self._run(self._get, job_id, include_events)

    def _list(self, limit: int) -> list[JobRecord]:
        rows = self._connection.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_record(row) for row in rows]

    async def list(self, limit: int = 50) -> list[JobRecord]:
        return await self._run(self._list, limit)

    def _expire(self, completed: int, failed: int, orphaned: int) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        with self._connection:
            rows = self._connection.execute(
                "SELECT id, state, updated_at FROM jobs WHERE state IN ('completed', 'delivered', 'failed', 'orphaned')"
            ).fetchall()
            for row in rows:
                if row["state"] == "completed":
                    continue
                age = (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
                retention = completed if row["state"] in {"completed", "delivered"} else failed
                if row["state"] == "orphaned":
                    retention = orphaned
                if age >= retention:
                    self._connection.execute(
                        """
                        UPDATE jobs
                           SET state = 'expired', request_json = '{}', response_json = NULL,
                               response_content = NULL, updated_at = ?
                         WHERE id = ?
                        """,
                        (_now(), row["id"]),
                    )
                    self._connection.execute("DELETE FROM job_events WHERE job_id = ?", (row["id"],))
                    count += 1
        return count

    async def expire(self, completed: int, failed: int, orphaned: int) -> int:
        return await self._run(self._expire, completed, failed, orphaned)

    async def close(self) -> None:
        await asyncio.to_thread(self._connection.close)
