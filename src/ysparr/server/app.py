"""FastAPI application for the Ysparr service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from ysparr import __version__
from ysparr.apmatia import ApmatiaPublicClient
from ysparr.config import Config
from ysparr.core.jobs import JobManager
from ysparr.persistence.sqlite import SQLiteJobStore
from ysparr.persistence.store import JobStore
from ysparr.server.admin_routes import router as admin_router
from ysparr.server.openai_routes import router as openai_router
from ysparr.upstream import OpenAIHTTPAdapter, UpstreamAdapter


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
        if self.coding is not None and request.get("model") == self.coding_model:
            return self.coding
        if self.chat_model and request.get("model") == self.chat_model:
            return self.chat
        return self.chat

    async def complete(self, request):
        return await self._target(request).complete(request)

    def stream(self, request):
        return self._target(request).stream(request)


def create_app(
    config: Config | None = None,
    adapter: UpstreamAdapter | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    """Construct the HTTP application without starting a server."""

    runtime_config = config or Config()
    runtime_adapter = adapter or RoutedUpstreamAdapter(runtime_config)
    runtime_store = store or SQLiteJobStore(runtime_config.database_path)
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
    app.state.apmatia_client = (
        ApmatiaPublicClient(runtime_config.apmatia_public_base_url)
        if runtime_config.apmatia_public_base_url
        else None
    )
    app.state.adapter = runtime_adapter
    app.state.job_store = runtime_store
    app.state.job_manager = runtime_manager
    app.include_router(openai_router)
    app.include_router(admin_router)

    @app.get("/health")
    @app.get("/ysparr/v1/status")
    def health() -> dict[str, str]:
        return {"service": "ysparr", "status": "ok", "version": __version__}

    return app
