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
from ysparr.security import IdentityStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ysparr", description="Standalone Ysparr service")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("serve", help="start the HTTP service")
    commands.add_parser("status", help="check the local service health endpoint")
    config = commands.add_parser(
        "config",
        help="show settings and validate them (default action: check)",
        description="Show effective settings. Running 'ysparr config' is equivalent to 'ysparr config check'.",
        epilog="Use 'ysparr config check' to state the action explicitly.",
    )
    config.add_subparsers(dest="config_command", metavar="ACTION").add_parser(
        "check", help="validate configuration"
    )
    commands.add_parser("jobs", help="list durable jobs")
    users = commands.add_parser("user", help="manage local API principals")
    user_commands = users.add_subparsers(dest="user_command")
    create_user = user_commands.add_parser("create", help="create a local user")
    create_user.add_argument("name")
    keys = commands.add_parser("key", help="manage client API keys")
    key_commands = keys.add_subparsers(dest="key_command")
    create_key = key_commands.add_parser("create", help="issue an API key")
    create_key.add_argument("user")
    create_key.add_argument("label")
    create_key.add_argument("--models", default="*", help="comma-separated model IDs or *")
    create_policy = create_key.add_mutually_exclusive_group()
    create_policy.add_argument("--public", action="store_true", help="restrict this key to chat only")
    create_policy.add_argument("--capabilities", help="comma-separated capabilities: chat,jobs:read,jobs:cancel")
    create_key.add_argument("--admin", action="store_true", help="grant administrative privileges")
    list_keys = key_commands.add_parser("list", help="list key metadata")
    list_keys.add_argument("--user")
    policy = key_commands.add_parser("policy", help="modify a key's model and capability policy")
    policy.add_argument("id")
    policy.add_argument("--models", help="comma-separated model IDs or *")
    policy_options = policy.add_mutually_exclusive_group()
    policy_options.add_argument("--public", action="store_true", help="set chat-only capabilities")
    policy_options.add_argument("--capabilities", help="comma-separated capabilities: chat,jobs:read,jobs:cancel")
    for action in ("enable", "disable", "revoke", "rotate"):
        command = key_commands.add_parser(action, help=f"{action} an API key")
        command.add_argument("id")
        if action == "rotate":
            command.add_argument("--label")
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
    headers = {"Authorization": f"Bearer {config.admin_api_key}"} if config.admin_api_key else {}
    request = Request(f"http://{_status_host(config)}:{config.port}{path}", method=method, headers=headers)
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
    if args.command == "user" and args.user_command == "create":
        try:
            user_id = IdentityStore(config.database_path).create_user(args.name)
        except Exception as exc:
            print(f"ysparr: unable to create user: {exc}")
            return 1
        print(json.dumps({"id": user_id, "name": args.name}))
        return 0
    if args.command == "key" and args.key_command:
        identities = IdentityStore(config.database_path)
        try:
            if args.key_command == "create":
                if args.public:
                    capabilities = ("chat",)
                elif args.capabilities is not None:
                    capabilities = tuple(item for item in args.capabilities.split(",") if item)
                else:
                    capabilities = ("chat", "jobs:read", "jobs:cancel")
                secret, key_id = identities.create_key(args.user, args.label, tuple(x for x in args.models.split(",") if x), capabilities, args.admin)
                print(json.dumps({"id": key_id, "key": secret, "warning": "Save this key now; it is shown only once."}))
            elif args.key_command == "list":
                print(json.dumps(identities.list_keys(args.user)))
            elif args.key_command == "policy":
                capabilities = None
                if args.public:
                    capabilities = ("chat",)
                elif args.capabilities is not None:
                    capabilities = tuple(item for item in args.capabilities.split(",") if item)
                models = tuple(item for item in args.models.split(",") if item) if args.models is not None else None
                identities.update_policy(args.id, models=models, capabilities=capabilities)
                metadata = next(key for key in identities.list_keys() if key["id"] == args.id)
                print(json.dumps({"id": args.id, "models": metadata["models"].splitlines(), "capabilities": metadata["capabilities"].splitlines()}))
            elif args.key_command == "rotate":
                old = next((key for key in identities.list_keys() if key["id"] == args.id), None)
                if old is None:
                    raise ValueError("unknown key")
                secret, key_id = identities.create_key(old["name"], args.label or old["label"], tuple(old["models"].splitlines()), tuple(old["capabilities"].splitlines()), bool(old["admin"]))
                identities.set_key(args.id, "revoke")
                print(json.dumps({"id": key_id, "key": secret, "revoked": args.id, "warning": "Save this replacement key now; it is shown only once."}))
            else:
                identities.set_key(args.id, args.key_command)
                print(json.dumps({"id": args.id, "status": args.key_command}))
        except Exception as exc:
            print(f"ysparr: unable to manage key: {exc}")
            return 1
        return 0
    if args.command == "status":
        try:
            with urlopen(f"http://{_status_host(config)}:{config.port}/health", timeout=2) as response:
                print(response.read().decode())
        except (HTTPError, URLError) as exc:
            print(f"ysparr: service unavailable: {exc.reason}")
            return 1
        return 0
    if args.command == "config" and args.config_command in {None, "check"}:
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
