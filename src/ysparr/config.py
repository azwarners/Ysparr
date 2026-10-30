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
    chat_model: str | None = None
    coding_base_url: str | None = None
    coding_model: str | None = None
    database_path: str = "~/.local/share/ysparr/ysparr.sqlite3"
    completed_retention_seconds: int = 7 * 86400
    failed_retention_seconds: int = 86400
    orphaned_retention_seconds: int = 30 * 86400
    apmatia_public_base_url: str | None = None

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
        if (self.coding_base_url is None) != (self.coding_model is None):
            raise ConfigError("coding_base_url and coding_model must be configured together")
        for name, value in (("chat_model", self.chat_model), ("coding_model", self.coding_model)):
            if value is not None and (not value.strip() or any(char.isspace() for char in value)):
                raise ConfigError(f"{name} must be a non-empty model ID without whitespace")
        if self.coding_base_url is not None:
            coding = urlparse(self.coding_base_url)
            if coding.scheme not in {"http", "https"} or not coding.netloc:
                raise ConfigError("coding_base_url must be an http(s) URL")
            if (
                coding.username
                or coding.password
                or coding.query
                or coding.fragment
                or coding.path not in {"", "/", "/v1", "/v1/"}
            ):
                raise ConfigError("coding_base_url must be an API root URL without credentials, query, or fragment")
            if self.chat_model == self.coding_model:
                raise ConfigError("chat_model and coding_model must be different so clients can select each endpoint")
        if self.apmatia_public_base_url is not None:
            public = urlparse(self.apmatia_public_base_url)
            if public.scheme not in {"http", "https"} or not public.netloc:
                raise ConfigError("apmatia_public_base_url must be an http(s) URL")
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
    chat_model = values.get("YSPARR_CHAT_MODEL") or None
    coding_base_url = values.get("YSPARR_CODING_BASE_URL") or None
    coding_model = values.get("YSPARR_CODING_MODEL") or None
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
        chat_model=chat_model,
        coding_base_url=coding_base_url.rstrip("/") if coding_base_url else None,
        coding_model=coding_model,
        apmatia_public_base_url=(values.get("YSPARR_APMATIA_PUBLIC_BASE_URL") or "").rstrip("/") or None,
        database_path=database_path,
        completed_retention_seconds=completed_retention_seconds,
        failed_retention_seconds=failed_retention_seconds,
        orphaned_retention_seconds=orphaned_retention_seconds,
    )
