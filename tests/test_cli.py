import pytest

from ysparr.cli import main


def test_cli_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    assert "serve" in capsys.readouterr().out


def test_status_uses_loopback_for_wildcard_host(monkeypatch, capsys) -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok"}'

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setenv("YSPARR_HOST", "0.0.0.0")
    monkeypatch.setenv("YSPARR_AUTH_ENABLED", "true")
    monkeypatch.setattr("ysparr.cli.urlopen", fake_urlopen)
    assert main(["status"]) == 0
    assert calls == [("http://127.0.0.1:8000/health", 2)]
    assert "ok" in capsys.readouterr().out


def test_jobs_and_job_commands_use_admin_api(monkeypatch, capsys) -> None:
    calls = []

    def fake_admin(config, path, method="GET"):
        calls.append((path, method))
        return {"data": [], "state": "cancelled"}

    monkeypatch.setattr("ysparr.cli._admin_request", fake_admin)
    assert main(["jobs"]) == 0
    assert main(["job", "show", "job-1"]) == 0
    assert main(["job", "cancel", "job-1"]) == 0
    assert calls == [
        ("/ysparr/v1/jobs", "GET"),
        ("/ysparr/v1/jobs/job-1", "GET"),
        ("/ysparr/v1/jobs/job-1", "DELETE"),
    ]
    assert capsys.readouterr().out.count("state") == 3


def test_config_check_does_not_print_api_key(monkeypatch, capsys) -> None:
    monkeypatch.setenv("YSPARR_UPSTREAM_API_KEY", "do-not-print-this")
    assert main(["config", "check"]) == 0
    output = capsys.readouterr().out
    assert "do-not-print-this" not in output
    assert '"upstream_api_key_configured": true' in output


def test_local_user_key_lifecycle_never_lists_plaintext(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("YSPARR_DATABASE_PATH", str(tmp_path / "keys.sqlite3"))
    assert main(["user", "create", "nick"]) == 0
    capsys.readouterr()
    assert main(["key", "create", "nick", "desktop", "--public", "--models", "chat-model"]) == 0
    created = capsys.readouterr().out
    import json

    created_data = json.loads(created)
    secret = created_data["key"]
    assert secret.startswith("ys_")
    assert main(["key", "list"]) == 0
    listed = capsys.readouterr().out
    assert secret not in listed
    assert "desktop" in listed
    assert main(["key", "policy", created_data["id"], "--models", "chat-model,new-model", "--capabilities", "chat,jobs:read"]) == 0
    updated = capsys.readouterr().out
    policy = json.loads(updated)
    assert policy["models"] == ["chat-model", "new-model"]
    assert policy["capabilities"] == ["chat", "jobs:read"]
    assert main(["key", "disable", created_data["id"]]) == 0
    capsys.readouterr()
    assert main(["key", "enable", created_data["id"]]) == 0
    capsys.readouterr()
    assert main(["key", "rotate", created_data["id"]]) == 0
    rotated = json.loads(capsys.readouterr().out)
    assert rotated["revoked"] == created_data["id"]
    assert rotated["id"] != created_data["id"]
    assert rotated["key"] != secret
    assert main(["key", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    original = next(row for row in rows if row["id"] == created_data["id"])
    replacement = next(row for row in rows if row["id"] == rotated["id"])
    assert original["revoked"] is True
    assert replacement["revoked"] is False
