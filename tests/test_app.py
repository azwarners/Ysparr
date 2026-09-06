from ysparr.config import Config
from ysparr.server.app import create_app


def test_health_endpoint() -> None:
    app = create_app(Config())
    health_routes = [route for route in app.routes if route.path == "/health"]
    assert len(health_routes) == 1
    assert health_routes[0].endpoint() == {
        "service": "ysparr",
        "status": "ok",
        "version": "0.1.0",
    }


def test_status_endpoint() -> None:
    app = create_app()
    status_routes = [route for route in app.routes if route.path == "/ysparr/v1/status"]
    assert len(status_routes) == 1
    assert status_routes[0].endpoint()["status"] == "ok"
