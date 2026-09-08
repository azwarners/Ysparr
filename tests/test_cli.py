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
