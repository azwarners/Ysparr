"""Standalone Ysparr configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when Ysparr configuration is invalid."""


@dataclass(frozen=True)
class Config:
    """Runtime settings for the Ysparr service."""

    host: str = "127.0.0.1"
    port: int = 8000
    upstream_base_url: str = "http://127.0.0.1:4000"
    upstream_api_key: str | None = None

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ConfigError("host must not be empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ConfigError("port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ConfigError("port must be between 1 and 65535")
        parsed = urlparse(self.upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("upstream_base_url must be an http(s) URL")


def load_config(environ: dict[str, str] | None = None) -> Config:
    """Load configuration, applying YSPARR_* environment overrides."""

    values = os.environ if environ is None else environ
    host = values.get("YSPARR_HOST", Config.host)
    port_value = values.get("YSPARR_PORT", str(Config.port))
    upstream_base_url = values.get("YSPARR_UPSTREAM_BASE_URL", Config.upstream_base_url)
    upstream_api_key = values.get("YSPARR_UPSTREAM_API_KEY") or None
    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("YSPARR_PORT must be an integer") from exc
    return Config(
        host=host,
        port=port,
        upstream_base_url=upstream_base_url.rstrip("/"),
        upstream_api_key=upstream_api_key,
    )
