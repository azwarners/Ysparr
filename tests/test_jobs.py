import asyncio

import httpx

from ysparr.config import Config
from ysparr.core.jobs import JobManager
from ysparr.persistence.sqlite import SQLiteJobStore
from ysparr.server.app import create_app
from ysparr.upstream.base import UpstreamError


class CompletionAdapter:
    async def list_models(self):
        return {"object": "list", "data": []}

    async def complete(self, request):
        return {"id": "complete", "choices": [{"message": {"content": "done"}}]}

    async def _stream(self, request):
        yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n'

    def stream(self, request):
        return self._stream(request)


class SlowCompletionAdapter(CompletionAdapter):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        self.started.set()
        await self.release.wait()
        return await super().complete(request)


class SlowStreamAdapter(CompletionAdapter):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _stream(self, request):
        self.started.set()
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        await self.release.wait()
        yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n'


class FailingAdapter(CompletionAdapter):
    async def complete(self, request):
        raise UpstreamError("gateway failed", 503)


def run(coro):
    return asyncio.run(coro)


def test_sqlite_job_persists_and_reloads(tmp_path):
    path = str(tmp_path / "jobs.sqlite3")

    async def create_and_close():
        store = SQLiteJobStore(path)
        await store.create("job-1", {"model": "alias", "messages": []})
        await store.mark_running("job-1")
        await store.append_event("job-1", b"event")
        await store.complete("job-1", {"id": "response"}, "done", False)
        await store.close()

    run(create_and_close())

    async def reload():
        store = SQLiteJobStore(path)
        record = await store.get("job-1", include_events=True)
        await store.close()
        return record

    record = run(reload())
    assert record.state == "completed"
    assert record.response == {"id": "response"}
    assert record.events


def test_non_streaming_disconnect_survives_and_becomes_orphaned(tmp_path):
    adapter = SlowCompletionAdapter()
    store = SQLiteJobStore(str(tmp_path / "jobs.sqlite3"))
    manager = JobManager(store, adapter)

    async def scenario():
        job_id = await manager.submit({"model": "alias", "messages": [], "stream": False})
        await adapter.started.wait()
        await manager.mark_disconnected(job_id)
        adapter.release.set()
        return await manager.wait(job_id)

    record = run(scenario())
    assert record.state == "orphaned"
    assert record.response_content == "done"
    run(store.close())


def test_stream_disconnect_keeps_upstream_running_and_retains_events(tmp_path):
    adapter = SlowStreamAdapter()
    store = SQLiteJobStore(str(tmp_path / "jobs.sqlite3"))
    manager = JobManager(store, adapter)

    async def scenario():
        job_id = await manager.submit({"model": "alias", "messages": [], "stream": True})
        stream = manager.stream(job_id)
        first = await stream.__anext__()
        await stream.aclose()
        await adapter.started.wait()
        adapter.release.set()
        record = await manager.wait(job_id)
        return first, record

    first, record = run(scenario())
    assert first.startswith(b"data:")
    assert record.state == "orphaned"
    assert record.event_count == 2
    assert record.response_content == "partialdone"
    run(store.close())


def test_upstream_failure_is_retained(tmp_path):
    store = SQLiteJobStore(str(tmp_path / "jobs.sqlite3"))
    manager = JobManager(store, FailingAdapter())

    async def scenario():
        job_id = await manager.submit({"model": "alias", "messages": [], "stream": False})
        return await manager.wait(job_id)

    record = run(scenario())
    assert record.state == "failed"
    assert record.failure == "gateway failed"
    assert record.failure_status_code == 503
    run(store.close())


def test_restart_marks_running_job_failed_and_expiry_clears_payload(tmp_path):
    path = str(tmp_path / "jobs.sqlite3")

    async def setup():
        store = SQLiteJobStore(path)
        await store.create("running", {"model": "alias", "messages": []})
        await store.mark_running("running")
        await store.create("done", {"model": "alias", "messages": []})
        await store.complete("done", {"id": "response"}, "done", False)
        await store.mark_delivered("done")
        await store.close()
        store = SQLiteJobStore(path)
        await store.expire(0, 86400, 86400)
        running = await store.get("running")
        done = await store.get("done")
        await store.close()
        return running, done

    running, done = run(setup())
    assert running.state == "failed"
    assert "restart" in running.failure
    assert done.state == "expired"
    assert done.response is None
    assert done.request == {}


def test_admin_api_lists_inspects_and_cancels_jobs(tmp_path):
    adapter = SlowCompletionAdapter()
    app = create_app(Config(database_path=str(tmp_path / "jobs.sqlite3")), adapter)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            job_id = await app.state.job_manager.submit(
                {"model": "alias", "messages": [], "stream": False}
            )
            await adapter.started.wait()
            jobs = await client.get("/ysparr/v1/jobs")
            detail = await client.get(f"/ysparr/v1/jobs/{job_id}")
            cancelled = await client.delete(f"/ysparr/v1/jobs/{job_id}")
            response = await app.state.job_manager.wait(job_id)
            missing = await client.get("/ysparr/v1/jobs/missing")
            return jobs, detail, cancelled, response, missing

    jobs, detail, cancelled, response, missing = run(scenario())
    assert jobs.status_code == 200
    assert detail.json()["state"] == "running"
    assert cancelled.json()["state"] == "cancelled"
    assert response.state == "cancelled"
    assert missing.status_code == 404
