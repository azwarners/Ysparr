import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

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

    async def _chunks(self) -> AsyncIterator[dict[str, Any]]:
        yield {"id": "completion-1", "choices": [{"delta": {"content": "hello"}}]}
        yield {"id": "completion-1", "choices": [{"delta": {"content": " world"}}]}

    def stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
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


def test_health_and_status_endpoints_use_http_path() -> None:
    app = create_app(Config(), FakeAdapter())
    assert request(app, "GET", "/health").json()["status"] == "ok"
    assert request(app, "GET", "/ysparr/v1/status").json()["service"] == "ysparr"


def test_models_success() -> None:
    response = request(create_app(adapter=FakeAdapter()), "GET", "/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fake-model"


def test_models_upstream_error() -> None:
    response = request(create_app(adapter=BrokenAdapter()), "GET", "/v1/models")
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"
    assert "Traceback" not in response.text


def test_non_streaming_completion_forwards_and_returns_response() -> None:
    adapter = FakeAdapter()
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    response = request(create_app(adapter=adapter), "POST", "/v1/chat/completions", json=payload)
    assert response.status_code == 200
    assert response.json()["id"] == "completion-1"
    assert adapter.completed == [payload | {"stream": False}]


def test_invalid_completion_request() -> None:
    response = request(create_app(adapter=FakeAdapter()), "POST", "/v1/chat/completions", json={"model": "x"})
    assert response.status_code == 422


def test_completion_upstream_error() -> None:
    response = request(
        create_app(adapter=BrokenAdapter()),
        "POST",
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "completion unavailable"


def test_streaming_completion_relays_sse_chunks() -> None:
    adapter = FakeAdapter()
    response = request(
        create_app(adapter=adapter),
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("data:") == 3
    assert "[DONE]" in response.text
    assert json.loads(response.text.split("data: ", 1)[1].split("\n", 1)[0])["id"] == "completion-1"
