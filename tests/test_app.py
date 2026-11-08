import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ysparr.config import Config
from ysparr.extensions import ExtensionClient, ExtensionUnavailable
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


def _extension_app(tmp_path, adapter, handler):
    app = _app(tmp_path, adapter)
    app.state.extension_clients = [
        ExtensionClient("http://extension", transport=httpx.MockTransport(handler))
    ]
    return app


def _extension_handler(request):
    if request.method == "GET":
        return httpx.Response(
            200,
            json={
                "version": "1",
                "entries": [
                    {
                        "id": "Luna",
                        "name": "Luna",
                        "aliases": ["apmatia-agent-12"],
                        "owned_by": "apmatia",
                        "target": {"preferred_model_type": "openai_compatible"},
                    }
                ],
            },
        )
    body = json.loads(request.content)
    assert body["request"]["messages"] == [{"role": "user", "content": "hello"}]
    return httpx.Response(
        200,
        json={
            "version": "1",
            "contributions": [{"role": "system", "content": "Compiled Luna instructions."}],
            "target": {"preferred_model_type": "openai_compatible"},
        },
    )


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


def test_generic_extension_adds_catalog_and_system_contribution(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, _extension_handler)
    models = request(app, "GET", "/v1/models").json()["data"]
    assert {"id": "Luna", "object": "model", "owned_by": "apmatia"} in models
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "apmatia-agent-12", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert adapter.completed[0]["messages"][0] == {
        "role": "system",
        "content": "Compiled Luna instructions.",
    }
    assert adapter.completed[0]["model"] == "apmatia-agent-12"
    assert adapter.completed[0]["_ysparr_target"] == {"preferred_model_type": "openai_compatible"}


def test_generic_extension_stream_uses_existing_job_path(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, _extension_handler)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={
            "model": "Luna",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.text.count("data:") == 2
    assert adapter.completed[0]["_ysparr_target"]["preferred_model_type"] == "openai_compatible"


def test_context_extension_enriches_ordinary_model_without_selecting_backend(tmp_path) -> None:
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "1",
                    "entries": [
                        {
                            "id": "workspace-context",
                            "name": "Workspace context",
                            "aliases": [],
                            "owned_by": "context",
                            "applies_to": "all",
                            "target": {},
                        }
                    ],
                },
            )
        body = json.loads(request.content)
        assert body["model"] == "fake-model"
        return httpx.Response(
            200,
            json={
                "version": "1",
                "contributions": [{"role": "developer", "content": "Use workspace context."}],
                "target": {},
            },
        )

    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, handler)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert adapter.completed[0]["messages"] == [
        {"role": "developer", "content": "Use workspace context."},
        {"role": "user", "content": "hello"},
    ]
    assert "_ysparr_target" not in adapter.completed[0]


def test_context_extension_can_be_configured_as_required(tmp_path) -> None:
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "1",
                    "entries": [
                        {
                            "id": "required-context",
                            "name": "Required context",
                            "aliases": [],
                            "owned_by": "context",
                            "applies_to": "all",
                            "target": {},
                        }
                    ],
                },
            )
        return httpx.Response(503)

    adapter = FakeAdapter()
    app = _app(tmp_path, adapter)
    app.state.extension_clients = [
        ExtensionClient(
            "http://extension",
            transport=httpx.MockTransport(handler),
            context_failure="fail",
        )
    ]
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 503
    assert adapter.completed == []


def test_extension_collision_uses_first_registered_owner(tmp_path) -> None:
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.host)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "1",
                    "entries": [
                        {
                            "id": "shared",
                            "name": "first",
                            "aliases": [],
                            "owned_by": request.url.host,
                            "target": {"preferred_model_type": request.url.host},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "version": "1",
                "contributions": [{"role": "system", "content": request.url.host}],
                "target": {"preferred_model_type": request.url.host},
            },
        )

    adapter = FakeAdapter()
    app = _app(tmp_path, adapter)
    app.state.extension_clients = [
        ExtensionClient("http://first", transport=httpx.MockTransport(handler)),
        ExtensionClient("http://second", transport=httpx.MockTransport(handler)),
    ]
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "shared", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert calls == ["first", "first"]
    assert adapter.completed[0]["messages"][0]["content"] == "first"


def test_unknown_legacy_selector_remains_an_ordinary_upstream_model(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, lambda request: httpx.Response(503))
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "apmatia-agent-99", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert adapter.completed[0]["model"] == "apmatia-agent-99"


def test_selected_extension_failure_does_not_fall_back_to_upstream(tmp_path) -> None:
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "1",
                    "entries": [
                        {
                            "id": "owned-model",
                            "name": "Owned model",
                            "aliases": [],
                            "owned_by": "dummy",
                            "target": {"preferred_model_type": "chat"},
                        }
                    ],
                },
            )
        return httpx.Response(503)

    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, handler)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "owned-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    assert adapter.completed == []


def test_extension_rejects_duplicate_selectors_in_one_catalog() -> None:
    async def read_catalog():
        client = ExtensionClient(
            "http://extension",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "version": "1",
                        "entries": [
                            {
                                "id": "same",
                                "name": "First",
                                "aliases": [],
                                "owned_by": "dummy",
                                "target": {"preferred_model_type": "chat"},
                            },
                            {
                                "id": "other",
                                "name": "Second",
                                "aliases": ["same"],
                                "owned_by": "dummy",
                                "target": {"preferred_model_type": "chat"},
                            },
                        ],
                    },
                )
            ),
        )
        return await client.catalog()

    with pytest.raises(ExtensionUnavailable, match="colliding catalog selectors"):
        asyncio.run(read_catalog())


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
