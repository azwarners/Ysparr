"""FastAPI application for the Phase 0 Ysparr service."""

from fastapi import FastAPI

from ysparr import __version__
from ysparr.config import Config


def create_app(config: Config | None = None) -> FastAPI:
    """Construct the HTTP application without starting a server."""

    app = FastAPI(title="Ysparr", version=__version__)
    app.state.config = config

    @app.get("/health")
    @app.get("/ysparr/v1/status")
    def health() -> dict[str, str]:
        return {"service": "ysparr", "status": "ok", "version": __version__}

    return app
