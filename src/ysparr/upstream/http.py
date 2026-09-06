"""Transparent OpenAI-compatible HTTP upstream adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ysparr.config import Config
from ysparr.upstream.base import UpstreamError


class OpenAIHTTPAdapter:
    """Forward OpenAI-compatible requests to a configured gateway unchanged.

    A LiteLLM proxy is the intended v1 gateway, but this adapter deliberately
    treats it as an OpenAI-compatible HTTP service and does not interpret model
    names or provider-specific fields.
    """

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = config.upstream_base_url
        self.api_key = config.upstream_api_key
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    @staticmethod
    def _error(operation: str, response: httpx.Response) -> UpstreamError:
        message = response.text[:500] or response.reason_phrase
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                message = str(payload["error"].get("message") or message)
        except ValueError:
            pass
        status_code = response.status_code if 400 <= response.status_code <= 599 else 502
        return UpstreamError(f"{operation} failed: {message}", status_code=status_code)

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers(),
            timeout=timeout,
            transport=self.transport,
        )

    async def list_models(self) -> dict[str, Any]:
        try:
            async with self._client(timeout=10) as client:
                response = await client.get("/v1/models")
        except httpx.RequestError as exc:
            raise UpstreamError(f"model listing failed: {exc}") from exc
        if response.is_error:
            raise self._error("model listing", response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("model listing returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise UpstreamError("model listing returned an invalid response")
        return payload

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.post("/v1/chat/completions", json=request)
        except httpx.RequestError as exc:
            raise UpstreamError(f"completion failed: {exc}") from exc
        if response.is_error:
            raise self._error("completion", response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("completion returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("completion returned an invalid response")
        return payload

    async def _stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        try:
            async with self._client() as client:
                async with client.stream("POST", "/v1/chat/completions", json=request) as response:
                    if response.is_error:
                        raise self._error("stream", response)
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except httpx.RequestError as exc:
            raise UpstreamError(f"stream failed: {exc}") from exc

    def stream(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        return self._stream({**request, "stream": True})
