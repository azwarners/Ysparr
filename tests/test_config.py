import pytest

from ysparr.config import Config, ConfigError, load_config


def test_defaults() -> None:
    assert load_config({}) == Config(host="127.0.0.1", port=8000)


def test_environment_overrides() -> None:
    config = load_config(
        {
            "YSPARR_HOST": "0.0.0.0",
            "YSPARR_PORT": "9000",
            "YSPARR_UPSTREAM_BASE_URL": "http://gateway:4000/",
            "YSPARR_UPSTREAM_API_KEY": "secret",
        }
    )
    assert config.host == "0.0.0.0"
    assert config.port == 9000
    assert config.upstream_base_url == "http://gateway:4000"
    assert config.upstream_api_key == "secret"


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port(port: int) -> None:
    with pytest.raises(ConfigError):
        Config(port=port)


def test_invalid_environment_port() -> None:
    with pytest.raises(ConfigError, match="YSPARR_PORT"):
        load_config({"YSPARR_PORT": "nope"})


def test_invalid_upstream_url() -> None:
    with pytest.raises(ConfigError, match="upstream_base_url"):
        Config(upstream_base_url="not-a-url")
