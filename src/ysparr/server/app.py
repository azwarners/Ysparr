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
from ysparr.upstream import OpenAIHTTPAdapter, UpstreamAdapter


def create_app(
    config: Config | None = None,
    adapter: UpstreamAdapter | None = None,
    store: JobStore | None = None,
) -> FastAPI:
    """Construct the HTTP application without starting a server."""

    runtime_config = config or Config()
    runtime_adapter = adapter or OpenAIHTTPAdapter(runtime_config)
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
