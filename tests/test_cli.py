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
