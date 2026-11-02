"""Local principals, client API keys, and request authorization."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

CAPABILITIES = frozenset({"chat", "jobs:read", "jobs:cancel"})


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    models: tuple[str, ...]
    capabilities: tuple[str, ...]
    admin: bool = False

    def allows_model(self, model_id: str) -> bool:
        return "*" in self.models or model_id in self.models


class IdentityStore:
    """SQLite-backed users and keys. Only scrypt hashes and short fingerprints persist."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        expanded = path if path == ":memory:" else str(Path(path).expanduser())
        self.db = sqlite3.connect(expanded, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS ysparr_users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ysparr_keys (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES ysparr_users(id), label TEXT NOT NULL,
                    secret_hash BLOB NOT NULL, fingerprint TEXT NOT NULL, models TEXT NOT NULL,
                    capabilities TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
                    revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def create_user(self, name: str) -> str:
        if not name.strip():
            raise ValueError("user name must not be empty")
        user_id = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute("INSERT INTO ysparr_users(id,name) VALUES (?,?)", (user_id, name.strip()))
        return user_id

    def create_key(self, user: str, label: str, models: tuple[str, ...] = ("*",),
                   capabilities: tuple[str, ...] = ("chat", "jobs:read", "jobs:cancel"),
                   admin: bool = False) -> tuple[str, str]:
        self._validate_capabilities(capabilities)
        with self.lock, self.db:
            row = self.db.execute("SELECT id FROM ysparr_users WHERE id=? OR name=?", (user, user)).fetchone()
            if row is None:
                raise ValueError(f"unknown user: {user}")
            key_id = str(uuid.uuid4())
            secret = f"ys_{key_id}." + secrets.token_urlsafe(32)
            digest = hashlib.scrypt(secret.encode(), salt=key_id.encode(), n=2**14, r=8, p=1)
            self.db.execute(
                "INSERT INTO ysparr_keys(id,user_id,label,secret_hash,fingerprint,models,capabilities,admin) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    key_id, row["id"], label, digest, secret[-8:], "\n".join(models),
                    "\n".join(capabilities), int(admin),
                ),
            )
            return secret, key_id

    @staticmethod
    def _validate_capabilities(capabilities: tuple[str, ...]) -> None:
        unknown = set(capabilities) - CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")

    def update_policy(
        self,
        key_id: str,
        *,
        models: tuple[str, ...] | None = None,
        capabilities: tuple[str, ...] | None = None,
    ) -> None:
        if models is None and capabilities is None:
            raise ValueError("provide models and/or capabilities")
        if capabilities is not None:
            self._validate_capabilities(capabilities)
        assignments = []
        values: list[str] = []
        if models is not None:
            assignments.append("models=?")
            values.append("\n".join(models))
        if capabilities is not None:
            assignments.append("capabilities=?")
            values.append("\n".join(capabilities))
        values.append(key_id)
        with self.lock, self.db:
            cursor = self.db.execute(
                f"UPDATE ysparr_keys SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            if not cursor.rowcount:
                raise ValueError("unknown key")

    def authenticate(self, secret: str) -> Principal | None:
        prefix, separator, _secret_part = secret.partition(".")
        key_id = prefix.removeprefix("ys_") if separator else ""
        if not key_id:
            return None
        with self.lock:
            row = self.db.execute("SELECT k.*,u.name FROM ysparr_keys k JOIN ysparr_users u ON u.id=k.user_id WHERE k.id=? AND k.enabled=1 AND k.revoked=0", (key_id,)).fetchone()
        if row is None:
            return None
        digest = hashlib.scrypt(secret.encode(), salt=row["id"].encode(), n=2**14, r=8, p=1)
        if not hmac.compare_digest(digest, row["secret_hash"]):
            return None
        return Principal(
            row["user_id"], row["name"], tuple(row["models"].splitlines()),
            tuple(row["capabilities"].splitlines()), bool(row["admin"]),
        )

    def list_keys(self, user: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT k.id,k.label,k.fingerprint,k.enabled,k.revoked,k.admin,k.models,k.capabilities,u.name "
                "FROM ysparr_keys k JOIN ysparr_users u ON u.id=k.user_id "
                "WHERE (? IS NULL OR u.name=? OR u.id=?) ORDER BY k.created_at",
                (user, user, user),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for flag in ("enabled", "revoked", "admin"):
                item[flag] = bool(item[flag])
            result.append(item)
        return result

    def set_key(self, key_id: str, action: str) -> None:
        column = {"enable": ("enabled", 1), "disable": ("enabled", 0), "revoke": ("revoked", 1)}[action]
        with self.lock, self.db:
            cursor = self.db.execute(f"UPDATE ysparr_keys SET {column[0]}=? WHERE id=?", (column[1], key_id))
            if not cursor.rowcount:
                raise ValueError("unknown key")


async def principal_for_request(request: Request) -> Principal | None:
    if not request.app.state.config.auth_enabled:
        return None
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    principal = request.app.state.identity_store.authenticate(token) if scheme.lower() == "bearer" and token else None
    if principal is None:
        raise HTTPException(status_code=401, detail="valid bearer API key required", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = principal
    return principal


def require_capability(principal: Principal | None, capability: str) -> None:
    if principal is not None and not principal.admin and capability not in principal.capabilities:
        raise HTTPException(status_code=403, detail="key does not permit this operation")

