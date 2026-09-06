"""Small contract between Ysparr routes and an upstream provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class UpstreamError(RuntimeError):
    """A safe, client-facing description of an upstream failure."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class UpstreamAdapter(Protocol):
    async def list_models(self) -> dict[str, Any]: ...

    async def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def stream(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...
