"""LiteLLM-backed upstream adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from ysparr.config import Config
from ysparr.upstream.base import UpstreamError


def _jsonable(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    if isinstance(value, dict):
        return value
    raise UpstreamError("upstream returned an unsupported response")


class LiteLLMAdapter:
    """Use LiteLLM for completions and its OpenAI-compatible gateway for models."""

    def __init__(self, config: Config) -> None:
        self.base_url = config.upstream_base_url
        self.api_key = config.upstream_api_key

    def _completion_kwargs(self, request: dict[str, Any]) -> dict[str, Any]:
        kwargs = dict(request)
        kwargs.pop("api_base", None)
        kwargs.pop("api_key", None)
        kwargs["api_base"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        return kwargs

    async def list_models(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                response = await client.get("/v1/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"model listing failed: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise UpstreamError("model listing returned an invalid response")
        return payload

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            import litellm

            response = await litellm.acompletion(**self._completion_kwargs(request))
            return _jsonable(response)
        except Exception as exc:
            raise UpstreamError(f"completion failed: {exc}") from exc

    async def _stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            import litellm

            response = await litellm.acompletion(
                **self._completion_kwargs({**request, "stream": True})
            )
            async for chunk in response:
                yield _jsonable(chunk)
        except Exception as exc:
            raise UpstreamError(f"stream failed: {exc}") from exc

    def stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        return self._stream(request)
