import pytest

from ysparr.config import Config, ConfigError, load_config


def test_defaults() -> None:
    assert load_config({}) == Config(host="127.0.0.1", port=8000)


def test_environment_overrides() -> None:
    assert load_config({"YSPARR_HOST": "0.0.0.0", "YSPARR_PORT": "9000"}) == Config("0.0.0.0", 9000)


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port(port: int) -> None:
    with pytest.raises(ConfigError):
        Config(port=port)


def test_invalid_environment_port() -> None:
    with pytest.raises(ConfigError, match="YSPARR_PORT"):
        load_config({"YSPARR_PORT": "nope"})
