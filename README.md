# Ysparr

Ysparr is a standalone foundation for a durable conversational relay between OpenAI-compatible clients and AI backends. It is designed to own request lifecycle work independently from client connectivity while keeping provider integrations and application-specific behavior behind future boundaries.

## Phase 1

Ysparr now provides a transparent OpenAI-compatible relay: OpenAI-compatible client -> Ysparr -> LiteLLM -> AI. Phase 1 supports model listing and streaming or non-streaming chat completions. Durable disconnect handling, persistence, jobs, reconciliation, and extensions are planned for Phase 2 and are not implemented yet.

## Development

From this directory, create an environment and install the package with development dependencies. The checked-in `uv.lock` makes uv the intended reproducible development workflow:

```sh
uv sync --extra dev
source .venv/bin/activate
```

Alternatively, a regular virtualenv and `python -m pip install -e '.[dev]'` are sufficient.

Configure the LiteLLM gateway (the default is `http://127.0.0.1:4000`):

```sh
export YSPARR_UPSTREAM_BASE_URL=http://127.0.0.1:4000
export YSPARR_UPSTREAM_API_KEY=your-key-if-required
```

Start the service:

```sh
ysparr serve
```

The default bind address is `127.0.0.1:8000`. Set `YSPARR_HOST` and `YSPARR_PORT` to override it. Check the service with:

```sh
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
ysparr status
```

Point an OpenAI-compatible client at `http://127.0.0.1:8000/v1` and use the configured model. Example requests:

```sh
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"Hello"}]}'

curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Configuration can be validated without starting the service using `ysparr config check`; it reports only whether an API key is configured, never the key itself. When `YSPARR_HOST` is `0.0.0.0` or `::`, `ysparr status` probes the corresponding local loopback address.
