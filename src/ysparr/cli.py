"""Command-line interface for Ysparr."""

from __future__ import annotations

import argparse
import json
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from ysparr import __version__
from ysparr.config import ConfigError, load_config
from ysparr.server.app import create_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ysparr", description="Standalone Ysparr service")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("serve", help="start the HTTP service")
    commands.add_parser("status", help="check the local service health endpoint")
    config = commands.add_parser("config", help="inspect configuration")
    config.add_subparsers(dest="config_command").add_parser("check", help="validate configuration")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ysparr: invalid configuration: {exc}")
        return 2

    if args.command == "serve":
        uvicorn.run(create_app(config), host=config.host, port=config.port)
        return 0
    if args.command == "status":
        try:
            with urlopen(f"http://{config.host}:{config.port}/health", timeout=2) as response:
                print(response.read().decode())
        except URLError as exc:
            print(f"ysparr: service unavailable: {exc.reason}")
            return 1
        return 0
    if args.command == "config" and args.config_command == "check":
        print(json.dumps({"host": config.host, "port": config.port}))
        return 0

    _parser().print_help()
    return 0
