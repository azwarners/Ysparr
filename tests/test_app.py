import asyncio
import hashlib
import hmac
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


def test_private_agent_tool_call_is_bounded_and_returned_to_upstream(tmp_path) -> None:
    class ToolAdapter(FakeAdapter):
        async def complete(self, request):
            self.completed.append(request)
            if len(self.completed) == 1:
                return {
                    "id": "tool-call",
                    "choices": [{"message": {"role": "assistant", "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "memory.recall", "arguments": '{"query":"hello"}'},
                    }]}}],
                }
            return {"id": "final", "choices": [{"message": {"role": "assistant", "content": "done"}}]}

    def handler(extension_request):
        path = extension_request.url.path
        if path.endswith("/catalog"):
            return httpx.Response(200, json={"version": "1", "entries": [{
                "id": "apmatia-agent-12", "name": "private", "aliases": [],
                "owned_by": "apmatia", "target": {"preferred_model_type": "openai_compatible"},
            }]})
        if path.endswith("/tools"):
            return httpx.Response(200, json={"version": "1", "agent_id": 12, "tools": [{
                "name": "memory.recall", "description": "recall", "input_schema": {"type": "object"},
            }]})
        if path.endswith("/memory.recall"):
            return httpx.Response(200, json={"call_id": "call-1", "status": "success", "result": {"matches": []}})
        return httpx.Response(200, json={
            "version": "1", "contributions": [{"role": "system", "content": "private"}],
            "target": {"preferred_model_type": "openai_compatible"},
        })

    adapter = ToolAdapter()
    app = _extension_app(tmp_path, adapter, handler)
    response = request(app, "POST", "/v1/chat/completions", json={
        "model": "apmatia-agent-12", "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "done"
    assert len(adapter.completed) == 2
    assert adapter.completed[0]["tools"][0]["function"]["name"] == "memory.recall"
    assert adapter.completed[1]["messages"][-1]["role"] == "tool"
    assert adapter.completed[0]["_ysparr_target"] == {"preferred_model_type": "openai_compatible"}


def test_guessed_apmatia_agent_id_fails_closed(tmp_path) -> None:
    app = _extension_app(tmp_path, FakeAdapter(), _extension_handler)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "apmatia-agent-999", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 404


def test_extension_selector_cannot_shadow_an_upstream_model(tmp_path) -> None:
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "version": "1",
                    "entries": [
                        {
                            "id": "fake-model",
                            "name": "private agent",
                            "aliases": [],
                            "owned_by": "apmatia",
                            "target": {"preferred_model_type": "openai_compatible"},
                        }
                    ],
                },
            )
        raise AssertionError("the colliding extension selector was invoked")

    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, handler)
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert calls == ["GET"]
    assert adapter.completed[0]["model"] == "fake-model"


def test_two_user_private_agent_integration_and_public_key_boundary(tmp_path) -> None:
    service_key = "service-secret"
    private_by_principal: dict[str, str] = {}

    def handler(extension_request):
        supplied = extension_request.headers.get("authorization", "")
        assert supplied == f"Bearer {service_key}"
        principal = extension_request.headers.get("x-ysparr-principal")
        signature = extension_request.headers.get("x-ysparr-principal-signature")
        if principal is not None:
            expected = hmac.new(service_key.encode(), principal.encode(), hashlib.sha256).hexdigest()
            assert hmac.compare_digest(signature or "", expected)
        private_selector = private_by_principal.get(principal or "")
        entries = [
            {
                "id": "Public starter",
                "name": "Public starter",
                "aliases": [],
                "owned_by": "apmatia",
                "target": {"preferred_model_type": "openai_compatible"},
            }
        ]
        if private_selector:
            entries.append(
                {
                    "id": private_selector,
                    "name": private_selector,
                    "aliases": [],
                    "owned_by": "apmatia",
                    "target": {"preferred_model_type": "openai_compatible"},
                }
            )
        if extension_request.url.path.startswith("/api/public/ysparr/agents/"):
            agent_id = int(extension_request.url.path.split("/")[-2])
            return httpx.Response(200, json={"version": "1", "agent_id": agent_id, "tools": []})
        if extension_request.method == "GET":
            return httpx.Response(200, json={"version": "1", "entries": entries})
        payload = json.loads(extension_request.content)
        if payload["model"] not in {entry["id"] for entry in entries}:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "version": "1",
                "contributions": [{"role": "system", "content": f"Instructions for {payload['model']}"}],
                "target": {"preferred_model_type": "openai_compatible"},
            },
        )

    config = Config(
        database_path=str(tmp_path / "jobs.sqlite3"),
        auth_enabled=True,
        extension_base_urls=("http://extension",),
        extension_api_key=service_key,
    )
    adapter = FakeAdapter()
    app = create_app(config, adapter)
    app.state.extension_clients = [
        ExtensionClient("http://extension", service_key, transport=httpx.MockTransport(handler))
    ]
    identities = app.state.identity_store
    user_a = identities.create_user("user-a")
    user_b = identities.create_user("user-b")
    key_a, _ = identities.create_key(user_a, "personal-a", ("*",), ("chat",))
    key_b, _ = identities.create_key(user_b, "personal-b", ("*",), ("chat",))
    public_key, _ = identities.create_key(user_b, "public", ("fake-model", "Public starter"), ("chat",))
    private_by_principal[user_a] = "apmatia-agent-8"
    private_by_principal[user_b] = "apmatia-agent-9"

    def headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    own = request(
        app,
        "POST",
        "/v1/chat/completions",
        headers=headers(key_a),
        json={"model": "apmatia-agent-8", "messages": [{"role": "user", "content": "hello"}]},
    )
    cross_user = request(
        app,
        "POST",
        "/v1/chat/completions",
        headers=headers(key_b),
        json={"model": "apmatia-agent-8", "messages": [{"role": "user", "content": "hello"}]},
    )
    public_models = request(app, "GET", "/v1/models", headers=headers(public_key))
    public_private = request(
        app,
        "POST",
        "/v1/chat/completions",
        headers=headers(public_key),
        json={"model": "apmatia-agent-8", "messages": [{"role": "user", "content": "hello"}]},
    )
    unknown_private = request(
        app,
        "POST",
        "/v1/chat/completions",
        headers=headers(key_b),
        json={"model": "apmatia-agent-999", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert own.status_code == 200
    assert cross_user.status_code == 404
    assert public_models.status_code == 200
    assert {model["id"] for model in public_models.json()["data"]} == {"fake-model", "Public starter"}
    assert public_private.status_code == 403
    assert unknown_private.status_code == 404


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


def test_guessed_legacy_selector_fails_closed_when_extension_is_unavailable(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, lambda request: httpx.Response(503))
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "apmatia-agent-99", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 404
    assert adapter.completed == []


def test_ordinary_model_remains_available_when_extension_is_unavailable(tmp_path) -> None:
    adapter = FakeAdapter()
    app = _extension_app(tmp_path, adapter, lambda request: httpx.Response(503))
    response = request(
        app,
        "POST",
        "/v1/chat/completions",
        json={"model": "fake-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert adapter.completed[0]["model"] == "fake-model"


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
