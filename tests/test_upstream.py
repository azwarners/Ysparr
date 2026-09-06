import asyncio
import json

import httpx

from ysparr.config import Config
from ysparr.upstream.http import OpenAIHTTPAdapter


def test_completion_uses_gateway_url_and_preserves_model_alias() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "gateway-completion", "choices": []})

    adapter = OpenAIHTTPAdapter(
        Config(upstream_base_url="http://gateway.example:4100", upstream_api_key="gateway-key"),
        transport=httpx.MockTransport(handler),
    )
    payload = {
        "model": "department/model-alias-without-provider",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.1,
    }
    response = asyncio.run(adapter.complete(payload))

    assert response["id"] == "gateway-completion"
    assert seen["url"] == "http://gateway.example:4100/v1/chat/completions"
    assert seen["body"] == payload


def test_stream_uses_gateway_url_and_forwards_sse_bytes() -> None:
    seen = {}
    sse = b'data: {"id":"stream-1","choices":[]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    adapter = OpenAIHTTPAdapter(
        Config(upstream_base_url="http://gateway.example:4100"),
        transport=httpx.MockTransport(handler),
    )

    async def collect() -> list[bytes]:
        return [chunk async for chunk in adapter.stream({"model": "arbitrary-alias", "messages": []})]

    chunks = asyncio.run(collect())
    assert b"".join(chunks) == sse
    assert seen["url"] == "http://gateway.example:4100/v1/chat/completions"
    assert seen["body"]["model"] == "arbitrary-alias"
    assert seen["body"]["stream"] is True


def test_models_use_gateway_url() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"object": "list", "data": []})

    adapter = OpenAIHTTPAdapter(
        Config(upstream_base_url="http://gateway.example:4100"),
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(adapter.list_models()) == {"object": "list", "data": []}
    assert seen == ["http://gateway.example:4100/v1/models"]
