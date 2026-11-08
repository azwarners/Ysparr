"""Versioned, read-only external request extensions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx


class ExtensionUnavailable(RuntimeError):
    """The configured extension could not be reached or validated."""


class ExtensionEntryNotFound(ExtensionUnavailable):
    """The extension is reachable but does not own the requested selector."""


class ExtensionClient:
    """Small HTTP client for one operator-approved extension registration."""

    CONTRACT_VERSION = "1"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        transport=None,
        *,
        allowed_roles: frozenset[str] | None = None,
        context_failure: str = "skip",
    ) -> None:
        if context_failure not in {"skip", "fail"}:
            raise ValueError("context_failure must be 'skip' or 'fail'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.allowed_roles = allowed_roles or frozenset({"system", "developer"})
        self.context_failure = context_failure

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        missing_entry: bool = False,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise ExtensionUnavailable("configured extension is unavailable") from exc
        if response.status_code == 404 and missing_entry:
            raise ExtensionEntryNotFound("extension entry was not found")
        if response.is_error:
            raise ExtensionUnavailable("extension request failed")
        try:
            return response.json()
        except ValueError as exc:
            raise ExtensionUnavailable("extension returned invalid JSON") from exc

    async def catalog(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/api/public/ysparr/catalog")
        if not isinstance(result, dict) or result.get("version") != self.CONTRACT_VERSION:
            raise ExtensionUnavailable("extension returned an unsupported catalog contract")
        entries = result.get("entries")
        if not isinstance(entries, list):
            raise ExtensionUnavailable("extension returned an invalid catalog")
        valid = [entry for entry in entries if isinstance(entry, dict) and _valid_entry(entry)]
        if len(valid) != len(entries):
            raise ExtensionUnavailable("extension returned an invalid catalog entry")
        selectors = [selector for entry in valid for selector in (entry["id"], *entry["aliases"])]
        if len(selectors) != len(set(selectors)):
            raise ExtensionUnavailable("extension returned colliding catalog selectors")
        return valid

    async def resolve(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/api/public/ysparr/resolve",
            {
                "version": self.CONTRACT_VERSION,
                "model": model,
                # The copy makes the read-only boundary explicit and prevents
                # caller-owned nested lists from being reused after dispatch.
                "request": deepcopy(request),
            },
            missing_entry=True,
        )
        if not isinstance(result, dict) or result.get("version") != self.CONTRACT_VERSION:
            raise ExtensionUnavailable("extension returned an unsupported resolution contract")
        contributions = result.get("contributions", [])
        if not isinstance(contributions, list) or any(
            not _valid_contribution(item) for item in contributions
        ):
            raise ExtensionUnavailable("extension returned invalid contributions")
        if any(item["role"] not in self.allowed_roles for item in contributions):
            raise ExtensionUnavailable("extension returned an unauthorized contribution role")
        target = result.get("target", {})
        if not isinstance(target, dict) or not _valid_target(target):
            raise ExtensionUnavailable("extension returned invalid target metadata")
        return {"contributions": contributions, "target": target}


def matching_entry(entries: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    """Return the first catalog entry matching a primary ID or alias."""
    for entry in entries:
        if entry.get("applies_to", "selector") != "selector":
            continue
        if model == entry["id"] or model in entry["aliases"]:
            return entry
    return None


def matching_context_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first entry that opts into ordinary-request enrichment."""
    return next((entry for entry in entries if entry.get("applies_to") == "all"), None)


def _valid_entry(entry: dict[str, Any]) -> bool:
    aliases = entry.get("aliases", [])
    applies_to = entry.get("applies_to", "selector")
    target = entry.get("target", {})
    return (
        isinstance(entry.get("id"), str)
        and bool(entry["id"])
        and isinstance(entry.get("name"), str)
        and bool(entry["name"])
        and isinstance(aliases, list)
        and all(isinstance(alias, str) and alias for alias in aliases)
        and isinstance(entry.get("owned_by"), str)
        and bool(entry["owned_by"])
        and applies_to in {"selector", "all"}
        and isinstance(target, dict)
        and _valid_target(target)
        and (
            applies_to == "all"
            or (
                isinstance(target.get("preferred_model_type"), str)
                and bool(target["preferred_model_type"].strip())
            )
        )
        and set(entry) <= {
            "id",
            "name",
            "aliases",
            "owned_by",
            "target",
            "applies_to",
        }
    )


def _valid_target(target: dict[str, Any]) -> bool:
    return all(
        isinstance(key, str)
        and isinstance(value, (str, int, float, bool, type(None)))
        for key, value in target.items()
    )


def _valid_contribution(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("role") in {"system", "developer"}
        and isinstance(value.get("content"), str)
        and bool(value["content"].strip())
        and set(value) <= {"role", "content"}
    )
