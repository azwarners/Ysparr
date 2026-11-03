"""FastAPI application for the Ysparr service."""

from contextlib import asynccontextmanager

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
    runtime_manager = JobManager(
        runtime_store,
        runtime_adapter,
        runtime_config.completed_retention_seconds,
        runtime_config.failed_retention_seconds,
        runtime_config.orphaned_retention_seconds,
    )

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
    app.state.extension_clients = [
        ExtensionClient(url, runtime_config.extension_api_key)
        for url in runtime_config.extension_base_urls
    ]
    app.state.adapter = runtime_adapter
    app.state.job_store = runtime_store
    app.state.identity_store = identity_store
    app.state.job_manager = runtime_manager
    app.include_router(openai_router)
    app.include_router(admin_router)

    @app.get("/health")
    @app.get("/ysparr/v1/status")
    def health() -> dict[str, str]:
        return {"service": "ysparr", "status": "ok", "version": __version__}

    return app
