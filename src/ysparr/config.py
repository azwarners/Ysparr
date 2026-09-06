"""Standalone Ysparr configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when Ysparr configuration is invalid."""


@dataclass(frozen=True)
class Config:
    """Runtime settings for the Phase 0 service."""

    host: str = "127.0.0.1"
    port: int = 8000

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ConfigError("host must not be empty")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise ConfigError("port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ConfigError("port must be between 1 and 65535")


def load_config(environ: dict[str, str] | None = None) -> Config:
    """Load configuration, applying YSPARR_* environment overrides."""

    values = os.environ if environ is None else environ
    host = values.get("YSPARR_HOST", Config.host)
    port_value = values.get("YSPARR_PORT", str(Config.port))
    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("YSPARR_PORT must be an integer") from exc
    return Config(host=host, port=port)
