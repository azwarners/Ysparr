# Ysparr

## Versioned external extensions

Ysparr can combine upstream models with entries contributed by an operator-approved
external extension. The extension receives a read-only request view and returns
additive role-aware contributions plus target metadata; Ysparr keeps routing,
durability, and streaming ownership.

Configure both services, then set:

```sh
export YSPARR_EXTENSION_BASE_URLS=http://127.0.0.1:8001
ysparr serve
curl http://127.0.0.1:8000/v1/models
```

Use your actual Apmatia address and Ysparr port. Apmatia ships starter profiles **Mira the Software Engineer**, **Luna the Linux Admin**, **Rowan the Writer**, **Belle the General Assistant**, and **Ivy the Creative Partner**. Once installed, model-mapped and published, their names can appear alongside the upstream models. The startup bootstrap for fresh Apmatia installations may publish the starter set automatically; manual installation defaults to private unless publication is requested.

When the Apmatia extension is selected, it contributes the compiled system
instruction and a preferred model type. It does not expose private agent data,
edit client messages, or choose an inference backend. Legacy
`apmatia-agent-{id}` selectors remain accepted. Add `YSPARR_EXTENSION_API_KEY`
when the extension requires bearer authentication.


**Ysparr Sends Prompts And Receives Responses.**

Ysparr is lightweight middleware between OpenAI-compatible clients and OpenAI-compatible AI servers or gateways. It gives applications a shared place to handle the request/response path instead of making every application implement its own model networking.

```text
Chat client -> Ysparr -> OpenAI-compatible server -> model
```

Ysparr currently acts as a transparent relay for model discovery and streaming or non-streaming chat completions. Accepted chat completions are durable SQLite jobs, so Ysparr continues upstream work and retains output even when the downstream client disconnects.

Ysparr communicates with its upstream through the OpenAI-compatible HTTP API and does not depend on a particular inference server or gateway implementation.

Phase 2 does not yet reconcile orphaned responses into later conversations. Conversation lineage and reconciliation are planned for Phase 3.

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

### 2. Configure an upstream OpenAI-compatible endpoint

Point Ysparr at the base URL of an OpenAI-compatible AI server or gateway:

```sh
export YSPARR_UPSTREAM_BASE_URL=http://127.0.0.1:8080
```

Use the host and port exposed by your upstream service. If the upstream requires an API key:

```sh
export YSPARR_UPSTREAM_API_KEY='your-key'
```

For separate selectable chat and coding targets, set all three variables below:

```sh
export YSPARR_CHAT_MODEL=chat-model-id
export YSPARR_CODING_BASE_URL=http://127.0.0.1:8081
export YSPARR_CODING_MODEL=coding-model-id
```

Ysparr lists the configured IDs at `/v1/models` and routes requests for the
coding ID to the coding endpoint. Other model IDs continue to use the chat
endpoint. The chat API key is never sent to the coding endpoint.

Phase 2 durability configuration defaults are:

```text
YSPARR_DATABASE_PATH=~/.local/share/ysparr/ysparr.sqlite3
YSPARR_RETENTION_COMPLETED=7d
YSPARR_RETENTION_FAILED=24h
YSPARR_RETENTION_ORPHANED=30d
```

Set `YSPARR_DATABASE_PATH` for a different SQLite location. Retention values accept seconds (`3600`) or suffixes `s`, `m`, `h`, `d`, and `w`.

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

If `/v1/models` returns the models exposed by your upstream, Ysparr is ready for a client.

### 5. Connect a chat client

Configure any chat client that supports a custom OpenAI-compatible endpoint to use:

```text
http://127.0.0.1:8000/v1
```

Examples of chat applications in this general category include Open WebUI, LibreChat, LobeChat, Chatbox, LocalLightChat, and others. Ysparr is not tied to any particular client.

If your chat client runs in a container or on another machine, use the network address of the machine running Ysparr instead of `127.0.0.1`. To listen beyond localhost, set `YSPARR_HOST` appropriately before starting the service.

### Secure LAN access

Ysparr keeps its no-key localhost behavior for existing installations. Binding to a non-loopback address now requires `YSPARR_AUTH_ENABLED=true`; all `/v1/*` and `/ysparr/v1/*` routes then require a Ysparr client bearer key. Keep the service on localhost behind a TLS reverse proxy, or terminate TLS at a trusted gateway and restrict firewall access to trusted clients. Do not expose plain HTTP over an untrusted network.

Create a principal and an ordinary scoped key locally:

```sh
ysparr user create nick
ysparr key create nick workstation --models chat-model-id,public-starter-name
```

The key is printed once. Ysparr stores only a scrypt hash and a short fingerprint. `ysparr key list` shows metadata only; `ysparr key disable ID`, `enable ID`, `revoke ID`, and `rotate ID` take effect immediately. `ysparr key create USER LABEL --models model-a,StarterName` creates a key restricted to those model IDs. Its allowlist is enforced in `/v1/models` and at completion time, including guessed Apmatia IDs. Only currently published Apmatia agents can be resolved, and an agent must also be explicitly included in the key's model allowlist. `--public` creates a chat-only key with no job inspection or cancellation access. `--admin` grants a key administrative access to retained jobs, including historical jobs without an owner.

Update a key's policy without rotating its secret or restarting Ysparr:

```sh
ysparr key policy KEY_ID --models chat-model-id,public-starter-name --public
ysparr key policy KEY_ID --capabilities chat,jobs:read
```

Use either `--models` or `--capabilities` (or both) to update only the specified parts. Supported capabilities are `chat`, `jobs:read`, and `jobs:cancel`; an empty capability list denies all ordinary capabilities. Administrative privilege remains a separate setting.

Enable authenticated operation with:

```sh
export YSPARR_AUTH_ENABLED=true
export YSPARR_HOST=0.0.0.0
ysparr serve
```

Ordinary API keys can access only their own retained jobs when granted `jobs:read` or `jobs:cancel`; keys made with `--public` receive neither capability. Health endpoints remain available for local monitoring. Local key administration uses the SQLite database directly and requires no running service. For the legacy job CLI over an authenticated service, set `YSPARR_ADMIN_API_KEY` to an administrative bearer key in the shell environment. Client keys are separate from `YSPARR_UPSTREAM_API_KEY`, which is only sent to the configured upstream.

## What works today

- OpenAI-compatible `GET /v1/models`
- OpenAI-compatible `POST /v1/chat/completions`
- streaming chat completions
- non-streaming chat completions
- arbitrary model names and aliases exposed by the upstream
- configurable upstream URL and API key
- configurable Ysparr bind host and port
- transparent OpenAI-compatible HTTP forwarding through a replaceable upstream adapter
- SQLite-backed durable jobs
- continued upstream generation after client disconnect
- retained response content and streaming events
- job inspection through the CLI and administrative API
- active-job cancellation

## Configuration

Ysparr currently uses environment variables:

```text
YSPARR_HOST
YSPARR_PORT
YSPARR_UPSTREAM_BASE_URL
YSPARR_UPSTREAM_API_KEY
YSPARR_CHAT_MODEL
YSPARR_CODING_BASE_URL
YSPARR_CODING_MODEL
YSPARR_EXTENSION_BASE_URLS
YSPARR_MODEL_TYPE_ROUTES
YSPARR_EXTENSION_API_KEY
YSPARR_AUTH_ENABLED
YSPARR_ADMIN_API_KEY
YSPARR_DATABASE_PATH
YSPARR_RETENTION_COMPLETED
YSPARR_RETENTION_FAILED
YSPARR_RETENTION_ORPHANED
```

Defaults:

```text
YSPARR_HOST=127.0.0.1
YSPARR_PORT=8000
YSPARR_UPSTREAM_BASE_URL=http://127.0.0.1:4000
YSPARR_DATABASE_PATH=~/.local/share/ysparr/ysparr.sqlite3
YSPARR_RETENTION_COMPLETED=7d
YSPARR_RETENTION_FAILED=24h
YSPARR_RETENTION_ORPHANED=30d
YSPARR_AUTH_ENABLED=false
```

Set `YSPARR_UPSTREAM_BASE_URL` to the address of the OpenAI-compatible service you want Ysparr to use.

To expose explicitly published Apmatia agents as virtual OpenAI models, set
`YSPARR_EXTENSION_BASE_URLS` to one or more extension base URLs (comma-separated).
Ysparr reads the versioned, read-only catalog and resolves the selected entry at
request time. Agent names are used as model IDs; legacy `apmatia-agent-{id}` IDs
remain accepted. If an extension cannot be reached, Ysparr does not reuse stale
catalog data. Set `YSPARR_EXTENSION_API_KEY` when the extension requires bearer
authentication. Map extension target metadata to upstream model IDs with
`YSPARR_MODEL_TYPE_ROUTES`, for example:

```sh
export YSPARR_MODEL_TYPE_ROUTES='{"openai_compatible":"MiniMax"}'
```

CLI help is available at both levels:

```sh
ysparr --help
ysparr config --help
```

Validate configuration with:

```sh
ysparr config
```

The explicit subcommand remains available as an equivalent form:

```sh
ysparr config check
```

The command reports whether an upstream API key is configured but does not print the key.

Check a running local service with:

```sh
ysparr status
ysparr jobs
```

Inspect or cancel a job through the service:

```sh
ysparr job show JOB_ID
ysparr job cancel JOB_ID
```

The equivalent administrative API is:

```text
GET    /ysparr/v1/jobs
GET    /ysparr/v1/jobs/{id}
DELETE /ysparr/v1/jobs/{id}
```

Job listings contain metadata only. Job detail may include retained response data and raw streaming events.

When `YSPARR_HOST` is `0.0.0.0` or `::`, `ysparr status` probes the corresponding local loopback address.

## Manual disconnect test

1. Start a slow model behind the configured OpenAI-compatible gateway.
2. Start Ysparr and submit a long streaming request from LocalLightChat or Open WebUI.
3. Close the client before generation completes.
4. Confirm the gateway request continues, then run `ysparr jobs`.
5. Use `ysparr job show JOB_ID` to inspect the completed retained response/events.

Disconnecting a client does not cancel an accepted upstream generation.

Ysparr does not automatically insert that orphaned response into a later client conversation yet. That is Phase 3 work.

## Where Ysparr is going

Planned work includes:

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

A conventional virtual environment with:

```sh
python -m pip install -e '.[dev]'
```

also works.
