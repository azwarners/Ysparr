"""Standalone Ysparr configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when Ysparr configuration is invalid."""


def _duration(value: str, name: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value.lower())
    if not match:
        raise ConfigError(f"{name} must be a duration such as 30m, 7d, or 0")
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return amount * multiplier


@dataclass(frozen=True)
class Config:
    """Runtime settings for the Ysparr service."""

    host: str = "127.0.0.1"
    port: int = 8000
    upstream_base_url: str = "http://127.0.0.1:4000"
    upstream_api_key: str | None = None
    database_path: str = "~/.local/share/ysparr/ysparr.sqlite3"
    completed_retention_seconds: int = 7 * 86400
    failed_retention_seconds: int = 86400
    orphaned_retention_seconds: int = 30 * 86400

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
        if not self.database_path.strip():
            raise ConfigError("database_path must not be empty")
        for name, value in (
            ("completed_retention_seconds", self.completed_retention_seconds),
            ("failed_retention_seconds", self.failed_retention_seconds),
            ("orphaned_retention_seconds", self.orphaned_retention_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{name} must be a non-negative integer")


def load_config(environ: dict[str, str] | None = None) -> Config:
    """Load configuration, applying YSPARR_* environment overrides."""

    values = os.environ if environ is None else environ
    host = values.get("YSPARR_HOST", Config.host)
    port_value = values.get("YSPARR_PORT", str(Config.port))
    upstream_base_url = values.get("YSPARR_UPSTREAM_BASE_URL", Config.upstream_base_url)
    upstream_api_key = values.get("YSPARR_UPSTREAM_API_KEY") or None
    database_path = values.get("YSPARR_DATABASE_PATH", Config.database_path)
    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("YSPARR_PORT must be an integer") from exc
    completed_retention_seconds = _duration(
        values.get("YSPARR_RETENTION_COMPLETED", "7d"), "YSPARR_RETENTION_COMPLETED"
    )
    failed_retention_seconds = _duration(
        values.get("YSPARR_RETENTION_FAILED", "24h"), "YSPARR_RETENTION_FAILED"
    )
    orphaned_retention_seconds = _duration(
        values.get("YSPARR_RETENTION_ORPHANED", "30d"), "YSPARR_RETENTION_ORPHANED"
    )
    return Config(
        host=host,
        port=port,
        upstream_base_url=upstream_base_url.rstrip("/"),
        upstream_api_key=upstream_api_key,
        database_path=database_path,
        completed_retention_seconds=completed_retention_seconds,
        failed_retention_seconds=failed_retention_seconds,
        orphaned_retention_seconds=orphaned_retention_seconds,
    )
