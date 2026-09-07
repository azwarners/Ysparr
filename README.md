# Ysparr

**Ysparr Sends Prompts And Receives Responses.**

Ysparr is lightweight middleware between OpenAI-compatible clients and OpenAI-compatible AI gateways. It gives applications a shared place to handle the request/response path instead of making every application implement its own model networking.

```text
Chat client -> Ysparr -> AI gateway -> model
```

Ysparr currently acts as a transparent relay for model discovery and streaming or non-streaming chat completions. LiteLLM is the primary upstream used during development, but Ysparr talks to it through the OpenAI-compatible HTTP API rather than depending on LiteLLM itself.

Ysparr is designed to grow into a durable conversational relay that can keep long-running AI work alive across temporary client disconnects and let optional integrations add behavior around conversations. In the future, integrations could connect ordinary chat clients to applications such as terminal tools, agent systems, or CLI-based AI workers without requiring those applications to build their own chat interface.

## Quick start

### 1. Install Ysparr

```sh
git clone https://github.com/azwarners/Ysparr.git
cd Ysparr
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 2. Configure an upstream AI gateway

Ysparr expects an OpenAI-compatible upstream endpoint. LiteLLM running locally on its default port is one example:

```sh
export YSPARR_UPSTREAM_BASE_URL=http://127.0.0.1:4000
```

If the upstream requires an API key:

```sh
export YSPARR_UPSTREAM_API_KEY='your-key'
```

### 3. Start Ysparr

```sh
ysparr serve
```

By default Ysparr listens on:

```text
http://127.0.0.1:8000
```

### 4. Verify the connection

In another terminal:

```sh
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
```

If `/v1/models` returns the models exposed by your upstream gateway, Ysparr is ready for a client.

### 5. Connect a chat client

Configure any chat client that supports a custom OpenAI-compatible endpoint to use:

```text
http://127.0.0.1:8000/v1
```

Examples of chat applications in this general category include Open WebUI, LibreChat, LobeChat, Chatbox, and others. Ysparr is not tied to any particular client.

If your chat client runs in a container or on another machine, use the network address of the machine running Ysparr instead of `127.0.0.1`. To listen beyond localhost, set `YSPARR_HOST` appropriately before starting the service.

## What works today

- OpenAI-compatible `GET /v1/models`
- OpenAI-compatible `POST /v1/chat/completions`
- streaming chat completions
- non-streaming chat completions
- arbitrary model aliases supplied by the upstream gateway
- configurable upstream URL and API key
- configurable Ysparr bind host and port
- transparent OpenAI-compatible HTTP forwarding through a replaceable upstream adapter

## Configuration

Ysparr currently uses environment variables:

```text
YSPARR_HOST
YSPARR_PORT
YSPARR_UPSTREAM_BASE_URL
YSPARR_UPSTREAM_API_KEY
```

Defaults:

```text
YSPARR_HOST=127.0.0.1
YSPARR_PORT=8000
YSPARR_UPSTREAM_BASE_URL=http://127.0.0.1:4000
```

Validate configuration with:

```sh
ysparr config check
```

The command reports whether an upstream API key is configured but does not print the key.

Check a running local service with:

```sh
ysparr status
```

## Where Ysparr is going

Planned work includes:

- keeping accepted model requests alive when clients disconnect
- retaining completed responses for reconnecting clients
- reconciling missed assistant responses into continuing conversations
- reusable integrations that can inspect, enrich, or react to the request/response path
- integrations that can bridge ordinary chat clients to more capable AI applications and CLI agents

The architectural blueprint and implementation plan live in [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md).

## Development

The repository includes `uv.lock` for reproducible development environments:

```sh
uv sync --extra dev
source .venv/bin/activate
pytest
```

A conventional virtual environment with `python -m pip install -e '.[dev]'` also works.
