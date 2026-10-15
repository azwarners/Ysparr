"""Read-only client for Apmatia's explicitly published prompt-only agents."""

from __future__ import annotations

from typing import Any

import httpx


MODEL_PREFIX = "apmatia-agent-"


def virtual_model_id(agent_id: int) -> str:
    return f"{MODEL_PREFIX}{agent_id}"


def named_model_id(agent_name: str) -> str:
    """Use the public agent name as the model ID for clients that ignore labels."""
    return agent_name


def parse_virtual_model_id(model: str) -> int | None:
    if not model.startswith(MODEL_PREFIX):
        return None
    suffix = model[len(MODEL_PREFIX) :]
    if not suffix.isdecimal() or int(suffix) < 1:
        raise ValueError("Invalid Apmatia agent model ID.")
    return int(suffix)


class ApmatiaPublicClient:
    """Fetches public records on demand; it deliberately keeps no stale cache."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = f"{base_url.rstrip('/')}/api/public/agents"
        self.transport = transport

    async def _get(self, path: str = "") -> Any:
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as client:
                response = await client.get(f"{self.url}{path}")
        except httpx.RequestError as exc:
            raise ApmatiaUnavailable("Apmatia public agents are unavailable.") from exc
        if response.is_error:
            if response.status_code == 404:
                raise ApmatiaAgentNotFound("Published Apmatia agent not found.")
            raise ApmatiaUnavailable("Apmatia public agents are unavailable.")
        try:
            return response.json()
        except ValueError as exc:
            raise ApmatiaUnavailable("Apmatia returned an invalid public-agent response.") from exc

    async def list_agents(self) -> list[dict[str, Any]]:
        result = await self._get()
        if not isinstance(result, list):
            raise ApmatiaUnavailable("Apmatia returned an invalid public-agent response.")
        return [agent for agent in result if isinstance(agent, dict) and _valid_agent(agent)]

    async def get_agent(self, agent_id: int) -> dict[str, Any]:
        result = await self._get(f"/{agent_id}")
        if not isinstance(result, dict) or not _valid_agent(result):
            raise ApmatiaUnavailable("Apmatia returned an invalid public-agent response.")
        return result


class ApmatiaUnavailable(RuntimeError):
    pass


class ApmatiaAgentNotFound(RuntimeError):
    pass


def _valid_agent(agent: dict[str, Any]) -> bool:
    return (
        isinstance(agent.get("id"), int)
        and isinstance(agent.get("name"), str)
        and isinstance(agent.get("system_prompt"), str)
        and bool(agent["system_prompt"])
    )
