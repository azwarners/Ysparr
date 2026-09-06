# Ysparr

Ysparr is a standalone foundation for a durable conversational relay between OpenAI-compatible clients and AI backends. It is designed to own request lifecycle work independently from client connectivity while keeping provider integrations and application-specific behavior behind future boundaries.

## Phase 0

Phase 0 provides the installable package, configuration, CLI, and a small health/status HTTP service. OpenAI proxying, LiteLLM, durable jobs, persistence, and extensions are not implemented yet.

## Development

From this directory, create an environment and install the package with development dependencies:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Start the service:

```sh
ysparr serve
```

The default bind address is `127.0.0.1:8000`. Set `YSPARR_HOST` and `YSPARR_PORT` to override it. Check the service with:

```sh
curl http://127.0.0.1:8000/health
ysparr status
```

Configuration can be validated without starting the service using `ysparr config check`.
