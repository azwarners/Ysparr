"""Command-line interface for Ysparr."""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
    commands.add_parser("jobs", help="list durable jobs")
    job = commands.add_parser("job", help="inspect or cancel a durable job")
    job_commands = job.add_subparsers(dest="job_command")
    show = job_commands.add_parser("show", help="show a durable job")
    show.add_argument("id")
    cancel = job_commands.add_parser("cancel", help="cancel an active durable job")
    cancel.add_argument("id")
    return parser


def _status_host(config) -> str:
    host = "127.0.0.1" if config.host in {"0.0.0.0", "::"} else config.host
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _admin_request(config, path: str, method: str = "GET") -> dict:
    request = Request(f"http://{_status_host(config)}:{config.port}{path}", method=method)
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode())


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
            with urlopen(f"http://{_status_host(config)}:{config.port}/health", timeout=2) as response:
                print(response.read().decode())
        except (HTTPError, URLError) as exc:
            print(f"ysparr: service unavailable: {exc.reason}")
            return 1
        return 0
    if args.command == "config" and args.config_command == "check":
        print(
            json.dumps(
                {
                    "host": config.host,
                    "port": config.port,
                    "upstream_base_url": config.upstream_base_url,
                    "upstream_api_key_configured": bool(config.upstream_api_key),
                    "database_path": config.database_path,
                    "retention_completed": f"{config.completed_retention_seconds}s",
                    "retention_failed": f"{config.failed_retention_seconds}s",
                    "retention_orphaned": f"{config.orphaned_retention_seconds}s",
                }
            )
        )
        return 0

    if args.command == "jobs":
        try:
            print(json.dumps(_admin_request(config, "/ysparr/v1/jobs")))
        except (HTTPError, URLError) as exc:
            print(f"ysparr: unable to query jobs: {exc.reason}")
            return 1
        return 0

    if args.command == "job" and args.job_command in {"show", "cancel"}:
        path = f"/ysparr/v1/jobs/{args.id}"
        try:
            print(json.dumps(_admin_request(config, path, "DELETE" if args.job_command == "cancel" else "GET")))
        except (HTTPError, URLError) as exc:
            print(f"ysparr: unable to query job: {exc.reason}")
            return 1
        return 0

    _parser().print_help()
    return 0
