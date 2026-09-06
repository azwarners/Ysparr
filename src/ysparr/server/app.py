"""FastAPI application for the Ysparr service."""

from fastapi import FastAPI

from ysparr import __version__
from ysparr.config import Config
from ysparr.server.openai_routes import router as openai_router
from ysparr.upstream import OpenAIHTTPAdapter, UpstreamAdapter


def create_app(config: Config | None = None, adapter: UpstreamAdapter | None = None) -> FastAPI:
    """Construct the HTTP application without starting a server."""

    app = FastAPI(title="Ysparr", version=__version__)
    app.state.config = config or Config()
    app.state.adapter = adapter or OpenAIHTTPAdapter(app.state.config)
    app.include_router(openai_router)

    @app.get("/health")
    @app.get("/ysparr/v1/status")
    def health() -> dict[str, str]:
        return {"service": "ysparr", "status": "ok", "version": __version__}

    return app
