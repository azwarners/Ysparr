import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ysparr.apmatia import ApmatiaPublicClient
from ysparr.config import Config
from ysparr.server.app import create_app
from ysparr.upstream.base import UpstreamError


class FakeAdapter:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []

    async def list_models(self) -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.completed.append(request)
        return {"id": "completion-1", "object": "chat.completion", "choices": []}

    async def _chunks(self) -> AsyncIterator[bytes]:
        yield b'data: {"id":"completion-1","choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield b'data: {"id":"completion-1","choices":[{"delta":{"content":" world"}}]}\n\n'

    def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        self.completed.append(request)
        return self._chunks()


class BrokenAdapter(FakeAdapter):
    async def list_models(self) -> dict[str, Any]:
        raise UpstreamError("gateway unavailable")

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        raise UpstreamError("completion unavailable")


def request(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def _app(tmp_path, adapter):
    return create_app(Config(database_path=str(tmp_path / "jobs.sqlite3")), adapter)


def test_health_and_status_endpoints_use_http_path(tmp_path) -> None:
    app = _app(tmp_path, FakeAdapter())
    assert request(app, "GET", "/health").json()["status"] == "ok"
    assert request(app, "GET", "/ysparr/v1/status").json()["service"] == "ysparr"


def test_models_success(tmp_path) -> None:
    response = request(_app(tmp_path, FakeAdapter()), "GET", "/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fake-model"


def test_models_upstream_error(tmp_path) -> None:
    response = request(_app(tmp_path, BrokenAdapter()), "GET", "/v1/models")
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "Traceback" not in response.text


def _with_apmatia(app, handler):
    app.state.apmatia_client = ApmatiaPublicClient("http://apmatia", httpx.MockTransport(handler))
    return app


def _agent_response(request):
    if request.url.path.endswith("/12"):
        return httpx.Response(200, json={
            "id": 12,
            "name": "Luna",
            "system_prompt": "Compiled Luna instructions.",
            "model": {"id": 4, "alias": "Fast", "backend": "openai", "model": "provider/model-x"},
        })
    return httpx.Response(200, json=[{
        "id": 12,
        "name": "Luna",
        "system_prompt": "Compiled Luna instructions.",
        "model": {"id": 4, "alias": "Fast", "backend": "openai", "model": "provider/model-x"},
    }])


def test_models_include_published_apmatia_agents(tmp_path) -> None:
    app = _with_apmatia(_app(tmp_path, FakeAdapter()), _agent_response)
    models = request(app, "GET", "/v1/models").json()["data"]
    assert models[0]["id"] == "fake-model"
    assert {"id": "Luna", "object": "model", "owned_by": "apmatia"} in models


def test_apmatia_agent_completion_composes_prompt_and_preserves_client_messages(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _with_apmatia(_app(tmp_path, adapter), _agent_response)
    messages = [
        {"role": "system", "content": "client policy"},
        {"role": "developer", "content": "client style"},
        {"role": "user", "content": "hello"},
    ]
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "Luna", "messages": messages,
    })
    assert response.status_code == 200
    assert adapter.completed[0]["model"] == "provider/model-x"
    assert adapter.completed[0]["messages"] == [
        {"role": "system", "content": "Compiled Luna instructions."}, *messages,
    ]


def test_legacy_apmatia_model_id_still_works(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _with_apmatia(_app(tmp_path, adapter), _agent_response)
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "apmatia-agent-12", "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 200
    assert adapter.completed[0]["model"] == "provider/model-x"


def test_apmatia_stream_uses_existing_job_path(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _with_apmatia(_app(tmp_path, adapter), _agent_response)
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "apmatia-agent-12", "messages": [{"role": "user", "content": "hello"}], "stream": True,
    })
    assert response.status_code == 200
    assert response.text.count("data:") == 2
    assert adapter.completed[0]["model"] == "provider/model-x"
    assert adapter.completed[0]["stream"] is True


def test_unknown_or_unavailable_apmatia_agent_fails_closed(tmp_path) -> None:
    app = _with_apmatia(_app(tmp_path, FakeAdapter()), lambda request: httpx.Response(404))
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "apmatia-agent-99", "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_apmatia_agent_without_model_mapping_fails_closed(tmp_path) -> None:
    app = _with_apmatia(
        _app(tmp_path, FakeAdapter()),
        lambda request: httpx.Response(200, json={
            "id": 12, "name": "Luna", "system_prompt": "Compiled Luna instructions.", "model": None,
        }),
    )
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "apmatia-agent-12", "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 422
    assert "no available model mapping" in response.json()["error"]["message"]


def test_unavailable_apmatia_hides_virtual_models_without_stale_data(tmp_path) -> None:
    app = _with_apmatia(_app(tmp_path, FakeAdapter()), lambda request: httpx.Response(503))
    models = request(app, "GET", "/v1/models").json()["data"]
    assert [model["id"] for model in models] == ["fake-model"]


def test_non_streaming_completion_forwards_and_returns_response(tmp_path) -> None:
    adapter = FakeAdapter()
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    app = _app(tmp_path, adapter)
    response = request(app, "POST", "/v1/chat/completions", json=payload)
    assert response.status_code == 200
    assert response.json()["id"] == "completion-1"
    assert adapter.completed == [payload | {"stream": False}]
    assert (request(app, "GET", "/ysparr/v1/jobs").json()["data"][0]["state"]) == "delivered"


def test_invalid_completion_request(tmp_path) -> None:
    response = request(_app(tmp_path, FakeAdapter()), "POST", "/v1/chat/completions", json={"model": "x"})
    assert response.status_code == 422


def test_completion_upstream_error(tmp_path) -> None:
    response = request(
        _app(tmp_path, BrokenAdapter()),
        "POST",
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "completion unavailable"


def test_streaming_completion_relays_sse_chunks(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _app(tmp_path, adapter)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data:") == 2
    assert "[DONE]" not in response.text
    assert '"id":"completion-1"' in response.text
    detail = request(app, "GET", "/ysparr/v1/jobs/" + request(app, "GET", "/ysparr/v1/jobs").json()["data"][0]["id"])
    assert detail.json()["state"] == "delivered"
    assert detail.json()["event_count"] == 2
