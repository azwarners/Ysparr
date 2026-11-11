"""FastAPI application for the Ysparr service."""

from contextlib import asynccontextmanager
import json
from collections.abc import AsyncIterator, Callable
from copy import deepcopy

from fastapi import FastAPI

from ysparr import __version__
from ysparr.config import Config
from ysparr.core.jobs import JobManager
from ysparr.persistence.sqlite import SQLiteJobStore
from ysparr.persistence.store import JobStore
from ysparr.server.admin_routes import router as admin_router
from ysparr.server.openai_routes import router as openai_router
from ysparr.security import IdentityStore
from ysparr.upstream import OpenAIHTTPAdapter, UpstreamAdapter
from ysparr.upstream.base import UpstreamError
from ysparr.extensions import ExtensionClient
from ysparr.extensions import ExtensionUnavailable


class BoundedToolAdapter:
    """Run the small, authenticated Apmatia memory-tool loop around an upstream."""

    MAX_TOOL_ROUNDS = 2

    def __init__(self, upstream: UpstreamAdapter, extensions: Callable[[], list[ExtensionClient]]) -> None:
        self.upstream = upstream
        self.extensions = extensions

    async def list_models(self):
        return await self.upstream.list_models()

    @staticmethod
    def _context(request: dict) -> tuple[dict, dict | None]:
        copied = deepcopy(request)
        return copied, copied.pop("_ysparr_tool_context", None)

    @staticmethod
    def _calls(response: dict) -> list[dict]:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return []
        message = choices[0].get("message")
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        return calls if isinstance(calls, list) else []

    async def complete(self, request: dict) -> dict:
        outbound, context = self._context(request)
        if not context:
            return await self.upstream.complete(outbound)
        messages = list(outbound.get("messages", []))
        for round_number in range(self.MAX_TOOL_ROUNDS + 1):
            outbound["messages"] = messages
            outbound["tools"] = context["tools"]
            response = await self.upstream.complete(outbound)
            calls = self._calls(response)
            if not calls:
                return response
            if round_number >= self.MAX_TOOL_ROUNDS:
                raise UpstreamError("bounded tool-call round limit exceeded", 422)
            assistant = response.get("choices", [{}])[0].get("message")
            if not isinstance(assistant, dict):
                raise UpstreamError("upstream returned an invalid tool-call message", 502)
            messages.append(assistant)
            for call in calls:
                result = await self._invoke(context, call)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, separators=(",", ":")),
                })
        raise UpstreamError("bounded tool-call round limit exceeded", 422)

    async def _invoke(self, context: dict, call: dict) -> dict:
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise UpstreamError("upstream returned an invalid tool call", 502)
        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise UpstreamError("upstream returned invalid tool arguments", 422) from exc
        if not isinstance(arguments, dict):
            raise UpstreamError("upstream returned invalid tool arguments", 422)
        extensions = self.extensions()
        index = context.get("extension_index")
        if not isinstance(index, int) or index < 0 or index >= len(extensions):
            raise UpstreamError("authorized tool endpoint is unavailable", 503)
        try:
            return await extensions[index].invoke_tool(
                context["agent_id"], function["name"], arguments,
                call_id=call.get("id"), principal_id=context.get("principal_id"),
            )
        except ExtensionUnavailable as exc:
            raise UpstreamError(str(exc), 503) from exc

    async def _buffer_stream(self, request: dict) -> tuple[list[bytes], dict]:
        chunks: list[bytes] = []
        calls: dict[int, dict] = {}
        buffer = ""
        async for chunk in self.upstream.stream(request):
            chunks.append(chunk)
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                for line in event.splitlines():
                    if not line.startswith("data: ") or line[6:] == "[DONE]":
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    choices = payload.get("choices") if isinstance(payload, dict) else None
                    delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
                    for tool_call in (delta or {}).get("tool_calls", []) if isinstance(delta, dict) else []:
                        if not isinstance(tool_call, dict):
                            continue
                        index = int(tool_call.get("index", 0))
                        current = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        current["id"] += str(tool_call.get("id") or "")
                        function = tool_call.get("function") or {}
                        current["function"]["name"] += str(function.get("name") or "")
                        current["function"]["arguments"] += str(function.get("arguments") or "")
        return chunks, {"tool_calls": [calls[index] for index in sorted(calls)]}

    def stream(self, request: dict) -> AsyncIterator[bytes]:
        return self._stream(request)

    async def _stream(self, request: dict) -> AsyncIterator[bytes]:
        outbound, context = self._context(request)
        if not context:
            async for chunk in self.upstream.stream(outbound):
                yield chunk
            return
        messages = list(outbound.get("messages", []))
        for round_number in range(self.MAX_TOOL_ROUNDS + 1):
            outbound["messages"] = messages
            outbound["tools"] = context["tools"]
            chunks, assembled = await self._buffer_stream(outbound)
            calls = assembled["tool_calls"]
            if not calls:
                for chunk in chunks:
                    yield chunk
                return
            if round_number >= self.MAX_TOOL_ROUNDS:
                raise UpstreamError("bounded tool-call round limit exceeded", 422)
            messages.append({"role": "assistant", "tool_calls": calls})
            for call in calls:
                result = await self._invoke(context, call)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        raise UpstreamError("bounded tool-call round limit exceeded", 422)


class RoutedUpstreamAdapter:
    """Expose configured chat/coding model IDs and dispatch each to its endpoint."""

    def __init__(self, config: Config, transport=None) -> None:
        self.chat = OpenAIHTTPAdapter(config, transport=transport)
        self.coding = None
        if config.coding_base_url and config.coding_model:
            from dataclasses import replace

            coding_base_url = config.coding_base_url.rstrip("/")
            if coding_base_url.endswith("/v1"):
                coding_base_url = coding_base_url[:-3]
            self.coding = OpenAIHTTPAdapter(
                # The existing chat credential is scoped to the chat endpoint;
                # never forward it to a separately configured host.
                replace(config, upstream_base_url=coding_base_url, upstream_api_key=None),
                transport=transport,
            )
        self.chat_model = config.chat_model
        self.coding_model = config.coding_model
        self.model_type_routes = config.model_type_routes or {}

    async def list_models(self):
        result = await self.chat.list_models()
        if self.chat_model or self.coding_model:
            models = result.get("data", [])
            if self.chat_model:
                models = [m for m in models if not isinstance(m, dict) or m.get("id") != self.chat_model]
                models.insert(0, {"id": self.chat_model, "object": "model", "owned_by": "ysparr-chat"})
            if self.coding_model and not any(isinstance(m, dict) and m.get("id") == self.coding_model for m in models):
                models.append({"id": self.coding_model, "object": "model", "owned_by": "ysparr-coding"})
            result["data"] = models
        return result

    def _target(self, request):
        target = request.get("_ysparr_target")
        if isinstance(target, dict):
            preferred = target.get("preferred_model_type")
            if preferred == "coding" and self.coding is not None:
                return self.coding
        if self.coding is not None and request.get("model") == self.coding_model:
            return self.coding
        if self.chat_model and request.get("model") == self.chat_model:
            return self.chat
        return self.chat

    async def complete(self, request):
        target = self._target(request)
        return await target.complete(self._resolved_request(request))

    def stream(self, request):
        target = self._target(request)
        return target.stream(self._resolved_request(request))

    def _resolved_request(self, request: dict) -> dict:
        result = _upstream_request(request)
        metadata = request.get("_ysparr_target")
        if isinstance(metadata, dict):
            preferred = metadata.get("preferred_model_type")
            if preferred in self.model_type_routes:
                result["model"] = self.model_type_routes[preferred]
            elif preferred == "coding" and self.coding_model:
                result["model"] = self.coding_model
            elif preferred == "chat" and self.chat_model:
                result["model"] = self.chat_model
            elif preferred:
                raise UpstreamError(
                    f"no upstream model route configured for preferred model type: {preferred}",
                    422,
                )
        return result


def _upstream_request(request: dict) -> dict:
    """Remove Ysparr-internal target metadata before forwarding upstream."""
    return {key: value for key, value in request.items() if key != "_ysparr_target"}


def create_app(
    config: Config | None = None,
    adapter: UpstreamAdapter | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    """Construct the HTTP application without starting a server."""

    runtime_config = config or Config()
    runtime_adapter = adapter or RoutedUpstreamAdapter(runtime_config)
    runtime_store = store or SQLiteJobStore(runtime_config.database_path)
    identity_store = IdentityStore(runtime_config.database_path)
    extension_clients: list[ExtensionClient] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime_store.expire(
            runtime_config.completed_retention_seconds,
            runtime_config.failed_retention_seconds,
            runtime_config.orphaned_retention_seconds,
        )
        try:
            yield
        finally:
            await runtime_manager.shutdown()

    app = FastAPI(title="Ysparr", version=__version__, lifespan=lifespan)
    app.state.config = runtime_config
    extension_clients.extend([
        ExtensionClient(
            url,
            runtime_config.extension_api_key,
            allowed_roles=frozenset(runtime_config.extension_allowed_roles),
            context_failure=runtime_config.extension_context_failure,
        )
        for url in runtime_config.extension_base_urls
    ])
    app.state.extension_clients = extension_clients
    runtime_adapter = BoundedToolAdapter(runtime_adapter, lambda: app.state.extension_clients)
    runtime_manager = JobManager(
        runtime_store,
        runtime_adapter,
        runtime_config.completed_retention_seconds,
        runtime_config.failed_retention_seconds,
        runtime_config.orphaned_retention_seconds,
    )
    app.state.adapter = runtime_adapter
    app.state.job_store = runtime_store
    app.state.identity_store = identity_store
    app.state.job_manager = runtime_manager
    app.include_router(openai_router)
    app.include_router(admin_router)

    @app.get("/health")
    @app.get("/ysparr/v1/status")
    async def health() -> dict[str, str]:
        return {"service": "ysparr", "status": "ok", "version": __version__}

    return app
