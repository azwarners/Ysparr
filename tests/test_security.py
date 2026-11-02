import asyncio
import sqlite3

import httpx

from ysparr.config import Config, ConfigError
from ysparr.persistence.sqlite import SQLiteJobStore
from ysparr.security import IdentityStore
from ysparr.server.app import create_app


class Adapter:
    async def list_models(self):
        return {"object": "list", "data": [{"id": "public-model"}, {"id": "private-model"}]}

    async def complete(self, request):
        return {"id": "ok", "choices": []}

    async def stream(self, request):
        if False:
            yield b""


def test_lan_binding_requires_authentication():
    try:
        Config(host="0.0.0.0")
    except ConfigError:
        pass
    else:
        raise AssertionError("wildcard bind must require explicit authentication")


def test_keys_are_hashed_and_revoke_without_restart(tmp_path):
    path = str(tmp_path / "ysparr.sqlite3")
    store = IdentityStore(path)
    user = store.create_user("nick")
    key, key_id = store.create_key(user, "lan", ("public-model",), ("chat",))
    row = store.db.execute("SELECT secret_hash,fingerprint FROM ysparr_keys WHERE id=?", (key_id,)).fetchone()
    assert key.encode() not in row["secret_hash"]
    assert key not in str(row["fingerprint"])
    assert store.authenticate(key).id == user
    store.db.close()
    store = IdentityStore(path)
    assert store.authenticate(key).id == user
    store.set_key(key_id, "revoke")
    assert store.authenticate(key) is None


def test_authenticated_model_filter_and_guessed_id_rejection(tmp_path):
    config = Config(database_path=str(tmp_path / "ysparr.sqlite3"), auth_enabled=True)
    app = create_app(config, Adapter())
    key, key_id = app.state.identity_store.create_key(
        app.state.identity_store.create_user("limited"), "public", ("public-model",), ("chat",)
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            missing = await client.get("/v1/models")
            headers = {"Authorization": f"Bearer {key}"}
            models = await client.get("/v1/models", headers=headers)
            denied = await client.post("/v1/chat/completions", headers=headers, json={
                "model": "private-model", "messages": [{"role": "user", "content": "hi"}],
            })
            cancel = await client.delete("/ysparr/v1/jobs/not-owned", headers=headers)
            openwebui_cancel = await client.post(
                "/ysparr/v1/openwebui/cancel", headers=headers, json={"chat_id": "private-chat"}
            )
            allowed = await client.post("/v1/chat/completions", headers=headers, json={
                "model": "public-model", "messages": [{"role": "user", "content": "hi"}],
            })
            jobs = await client.get("/ysparr/v1/jobs", headers=headers)
            app.state.identity_store.update_policy(key_id, models=("private-model",), capabilities=("chat",))
            updated_models = await client.get("/v1/models", headers=headers)
            old_model_denied = await client.post("/v1/chat/completions", headers=headers, json={
                "model": "public-model", "messages": [{"role": "user", "content": "hi"}],
            })
            return missing, models, denied, allowed, jobs, updated_models, old_model_denied, cancel, openwebui_cancel

    missing, models, denied, allowed, jobs, updated_models, old_model_denied, cancel, openwebui_cancel = asyncio.run(scenario())
    assert missing.status_code == 401
    assert [item["id"] for item in models.json()["data"]] == ["public-model"]
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert jobs.status_code == 403
    assert cancel.status_code == openwebui_cancel.status_code == 403
    assert [item["id"] for item in updated_models.json()["data"]] == ["private-model"]
    assert old_model_denied.status_code == 403


def test_users_cannot_inspect_or_cancel_each_others_jobs(tmp_path):
    config = Config(database_path=str(tmp_path / "ysparr.sqlite3"), auth_enabled=True)
    app = create_app(config, Adapter())
    identities = app.state.identity_store
    owner_id = identities.create_user("owner")
    stranger_id = identities.create_user("stranger")
    owner_key, _ = identities.create_key(owner_id, "owner", ("*",), ("chat", "jobs:read", "jobs:cancel"))
    stranger_key, _ = identities.create_key(stranger_id, "stranger", ("*",), ("chat", "jobs:read", "jobs:cancel"))

    async def scenario():
        await app.state.job_store.create("owned-job", {"model": "public-model", "messages": []}, owner_id)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {stranger_key}"}
            detail = await client.get("/ysparr/v1/jobs/owned-job", headers=headers)
            cancel = await client.delete("/ysparr/v1/jobs/owned-job", headers=headers)
            owner = await client.get("/ysparr/v1/jobs/owned-job", headers={"Authorization": f"Bearer {owner_key}"})
            return detail, cancel, owner

    detail, cancel, owner = asyncio.run(scenario())
    assert detail.status_code == cancel.status_code == 404
    assert owner.status_code == 200


def test_legacy_jobs_are_preserved_and_admin_only_after_migration(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (1);
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                state TEXT NOT NULL, model TEXT NOT NULL, request_json TEXT NOT NULL,
                response_json TEXT, response_content TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                orphaned INTEGER NOT NULL DEFAULT 0, disconnected INTEGER NOT NULL DEFAULT 0,
                failure TEXT, failure_status_code INTEGER, event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE job_events (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(job_id, sequence)
            );
            INSERT INTO jobs(id,created_at,updated_at,state,model,request_json,response_json,response_content,delivered)
            VALUES ('legacy-job','2026-01-01T00:00:00+00:00','2026-01-01T00:01:00+00:00','delivered',
                    'public-model','{"model":"public-model","messages":[]}',
                    '{"id":"old-response"}','preserved output',1);
        """)

    store = SQLiteJobStore(path)
    config = Config(database_path=path, auth_enabled=True)
    app = create_app(config, Adapter(), store)
    identities = app.state.identity_store
    identities.create_user("legacy-reader")
    user_key, _ = identities.create_key("legacy-reader", "ordinary", ("*",), ("chat", "jobs:read"))
    identities.create_user("administrator")
    admin_key, _ = identities.create_key("administrator", "admin", ("*",), (), admin=True)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ordinary = await client.get("/ysparr/v1/jobs/legacy-job", headers={"Authorization": f"Bearer {user_key}"})
            admin = await client.get("/ysparr/v1/jobs/legacy-job", headers={"Authorization": f"Bearer {admin_key}"})
            return ordinary, admin

    ordinary, admin = asyncio.run(scenario())
    assert ordinary.status_code == 404
    assert admin.status_code == 200
    assert admin.json()["response"] == {"id": "old-response"}
    assert admin.json()["response_content"] == "preserved output"
    assert asyncio.run(store.get("legacy-job")).principal_id is None
