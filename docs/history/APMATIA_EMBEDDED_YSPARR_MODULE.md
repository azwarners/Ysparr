# Historical Record: Apmatia’s Embedded Ysparr Module

Status: historical documentation

This document records the original Ysparr implementation that lived inside
Apmatia before the adapter migration described by Apmatia issue #4. It is
intentionally written as a technical inventory, not as current API guidance.

The snapshot is based on the Apmatia checkout at:

`/home/nick/ServerData/repos/apmatia/src/apmatia/modules/ysparr/`

The original implementation was an in-process text-generation subsystem. It
was not a client for the standalone Ysparr service. It contained request and
result types, modality execution, backend adapters, streaming response parsing,
file persistence, retention cleanup, configuration access, and provider error
types.

## 1. Original identity and stated purpose

The original manifest identified the module as:

```toml
module_id = "ysparr"
name = "Ysparr"
version = "0.1.0"
description = "Backend-agnostic generative execution and persistence infrastructure."
author = "Nick"
status = "stable"
category = "infrastructure"
default_enabled = true
tags = ["generative-ai", "execution", "streaming", "backends", "persistence"]
```

Its declared Python dependencies were `requests` and `jinja2`. The module
registered only metadata with Apmatia’s registry, but its package was directly
importable by application code and tests.

The original README described the product as “Ysparr Sends Prompts And
Receives Responses” and summarized its intended lifecycle as:

```text
Provide input → execute → receive output → persist result
```

The design was modality-oriented. The documentation anticipated text-to-text,
text-to-image, text-to-speech, image-to-image, and image-to-3D modalities, but
the implementation captured in this snapshot contained one concrete modality:
`text2text`.

## 2. Original package layout

The meaningful source layout was:

```text
src/apmatia/modules/ysparr/
├── __init__.py
├── module.py
├── manifest.toml
├── core/
│   ├── __init__.py
│   ├── config.py
│   ├── exceptions.py
│   └── types.py
└── modalities/
    ├── __init__.py
    └── text2text/
        ├── __init__.py
        ├── executor.py
        ├── storage.py
        ├── templates/
        │   ├── __init__.py
        │   └── chat_template.py
        └── backends/
            ├── __init__.py
            ├── koboldcpp_backend.py
            └── openai_compatible_backend.py
```

The original architecture documentation presented the conceptual flow as:

```text
Application / Module
        ↓
Modality Module (text2text, text2image, etc.)
        ↓
Core (types, utilities)
```

The concrete execution flow was more specific:

```text
PromptRequest
    ↓
text2text.execute
    ├── storage.initialize
    ├── backend.stream
    ├── storage.append for every emitted chunk
    └── storage.finalize
        ↓
ExecutionResult
```

## 3. Registry integration

`module.py` defined `APMATIA_YSPARR_MODULE` as a stable, default-enabled
infrastructure module and exposed:

```python
def register(registry: Registry) -> None:
    registry.register_module(APMATIA_YSPARR_MODULE)
```

The registry hook itself did not construct a backend, start a worker, create a
store, or register an HTTP route. The execution subsystem was activated by
direct imports of its modality and backend classes rather than by the registry
metadata function.

This distinction matters historically: the module was architecturally present
and importable even though its registry registration was metadata-only.

## 4. Core request and result types

### `PromptRequest`

`core/types.py` defined a frozen dataclass with these fields:

```python
PromptRequest(
    prompt_id: str,
    prompt_text: str,
    model_name: str,
    parameters: dict[str, Any] = {},
    metadata: dict[str, Any] = {},
    stop_event: threading.Event | None = None,
)
```

The fields had these practical meanings:

- `prompt_id` identified the execution and became the default output filename.
- `prompt_text` was the plain prompt used by the completion-style path and by
  the KoboldCpp fallback when no chat template was supplied.
- `model_name` was passed into OpenAI-compatible payloads as the model name;
  the literal value `default` caused the backend’s configured default model to
  be used.
- `parameters` carried generation settings such as `max_tokens`,
  `temperature`, `top_p`, penalties, `stop`, and `seed`.
- `metadata` was an open-ended control and callback channel. It carried chat
  messages, templates, persistence settings, retention settings, and optional
  event callbacks.
- `stop_event` allowed the executor and streaming backends to stop work
  cooperatively.

The dataclass was frozen, but its dictionary fields were not deeply immutable.
Consumers could therefore still mutate nested `parameters` or `metadata`
objects after construction.

### `ExecutionResult`

The result type was also frozen:

```python
ExecutionResult(
    prompt_id: str,
    status: str,
    output_path: str,
)
```

The executor returned `status="completed"` unless the stop event was observed,
in which case it returned `status="stopped"`. The result did not contain the
generated text itself; callers were expected to use the storage output path.

## 5. Configuration ownership

`core/config.py` was a thin facade over Apmatia’s shared application
configuration store:

- `load_config()` returned the complete Apmatia application configuration.
- `save_config(config)` persisted the complete configuration.
- `get_config_value(*keys, default=None)` read nested configuration values.

The Ysparr package did not own an independent configuration file or service
configuration namespace. The concrete backends read values under these nested
paths:

```text
text2text.openai_compatible.base_url
text2text.openai_compatible.api_key
text2text.openai_compatible.model_name
text2text.koboldcpp.base_url
```

The OpenAI-compatible backend also accepted constructor overrides, so callers
could avoid configuration-store lookup for `base_url`, `api_key`,
`model_name`, and timeout.

## 6. Exception hierarchy

`core/exceptions.py` defined a small synchronous exception hierarchy:

```text
YsparrError
├── ExecutionError
├── StorageError
└── BackendError
```

In practice, the executor and both concrete backends primarily raised
`ExecutionError`, while the text-file store raised `StorageError`. The
`BackendError` type existed as part of the public conceptual vocabulary but was
not the dominant error type in the concrete backend implementations.

The original errors were local Python exceptions. They were not translated to
HTTP status codes, durable job states, or a cross-process error contract.

## 7. Text-to-text executor

`modalities/text2text/executor.py` exposed:

```python
def execute(request: PromptRequest, backend, storage) -> ExecutionResult:
    ...
```

The executor required the caller to supply both a backend object and a storage
object. Its algorithm was:

1. Call `storage.initialize(request)` and retain the returned output path.
2. Iterate synchronously over `backend.stream(request)`.
3. Before appending each chunk, check `request.stop_event` when present.
4. Append the chunk with `storage.append(request, chunk)`.
5. Check the stop event again after appending.
6. Always call `storage.finalize(request)` in a `finally` block.
7. Return an `ExecutionResult` with the prompt ID, completion/stopped status,
   and output path.

The stop checks meant that a stop request could prevent a not-yet-appended
chunk from being written, or could stop immediately after a chunk had already
been appended. The executor did not call a backend-specific `stop` method.
That responsibility, where supported, belonged to the caller or backend
owner.

The executor did not provide:

- asynchronous execution;
- a durable job identifier separate from `prompt_id`;
- cancellation state persistence;
- reconnect or subscriber management;
- request authentication;
- model discovery;
- conversation history management;
- role-aware extension contributions;
- a service boundary.

## 8. Text-file persistence

`modalities/text2text/storage.py` implemented `TextFileStorage`. It was a
plain filesystem store, not a database-backed job store.

### File naming

For a request with ID `abc`, the output path was:

```text
<output_dir>/abc.txt
```

The output directory was created on demand. Unless
`metadata["append_existing"]` was true, initialization truncated or created
the file by writing an empty UTF-8 string.

### Persistence switch

If `metadata["persist"]` was false:

- `initialize()` returned an empty string;
- `append()` returned without writing;
- `finalize()` returned without checking or cleaning up a file.

The default was to persist.

### Streaming writes

Each emitted backend chunk opened the file in append mode, wrote the text, and
flushed the file handle. This favored visibility of partial output during a
long generation, at the cost of opening and closing the file for every chunk.

### Retention

`TextFileStorage` accepted an optional constructor-level `max_files` limit.
On finalization it enumerated `*.txt` files by modification time and deleted
oldest files until the global count was within the limit.

It also supported a request-level `metadata["max_age_seconds"]` value. Files
older than the requested age were deleted during finalization.

Retention was best-effort filesystem cleanup. It did not coordinate with
active executions, maintain job metadata, distinguish completed from failed
requests, or preserve event boundaries.

### Failure behavior

Filesystem errors were wrapped in `StorageError`. Finalization raised a
`StorageError` if persistence was enabled but the expected output file did not
exist. Because finalization was called by the executor’s `finally` block, a
storage finalization failure could replace or obscure an earlier backend
failure.

## 9. OpenAI-compatible backend

`backends/openai_compatible_backend.py` implemented
`OpenAICompatibleBackend` using the synchronous `requests` library.

### Endpoint selection

When `request.metadata["chat_messages"]` was a list, the backend first tried:

```text
POST <base_url>/v1/chat/completions
```

When chat execution failed with an error that looked like a missing or
unsupported chat endpoint—status `400` or `404`, “not found”, “bad request”, or
an error mentioning `chat/completions`—it fell back to:

```text
POST <base_url>/v1/completions
```

If no chat message list was present, it used the completion endpoint directly.
Multimodal messages disabled this fallback so an image request would not be
silently flattened into a text completion.

### Chat payload

The default chat payload included:

```json
{
  "model": "...",
  "messages": [],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 8192
}
```

Only an allowlist of generation parameters from `request.parameters` was
copied into the payload:

```text
max_tokens, temperature, top_p, frequency_penalty,
presence_penalty, stop, seed
```

### Completion payload

The completion fallback used the same defaults but replaced `messages` with
the plain `request.prompt_text` under `prompt`.

### Model resolution

If `request.model_name` was non-empty and not `default`, it was sent as the
model. Otherwise, the backend used its configured `model_name`, falling back
to the literal `gpt-4o-mini` if no model was configured.

### Streaming protocol

The backend performed a streaming `requests.post()` and iterated response
lines. It ignored empty lines and `[DONE]` events. It extracted text from
several provider shapes:

- chat `choices[0].delta.content`;
- completion `choices[0].text`;
- nonstandard `choices[0].message.content`.

Each extracted text fragment was yielded to the executor. If
`metadata["on_event"]` was callable, the backend also sent a structured event
containing provider name, endpoint, raw decoded event, extracted text, and
usage statistics when available.

### Stop handling

The backend checked `stop_event` while reading lines and closed the active HTTP
response when the event was set. It tracked active responses by `prompt_id` in
an in-memory dictionary protected by a `threading.Lock`. Its public `stop()`
method looked up the response and closed it.

This was process-local cancellation. It did not survive process failure and
was not connected to a durable job record.

### Docker host handling

When running in Docker, loopback base URLs such as `localhost` or
`127.0.0.1` were rewritten to the detected default-route gateway, or to
`host.docker.internal` when no gateway could be found. Non-loopback private
addresses were deliberately left unchanged. The behavior could also be
observed through `APMATIA_DEBUG_BACKEND=1` diagnostic prints.

### Error handling

`requests.RequestException` was converted to `ExecutionError` with a message
that included the provider failure. HTTP status failures were therefore
reported to the caller as local execution errors; there was no standardized
upstream status model.

## 10. KoboldCpp backend

`backends/koboldcpp_backend.py` implemented `KoboldCppBackend` using the
KoboldCpp API:

```text
POST <base_url>/api/v1/generate
```

The backend built a payload containing:

```json
{
  "prompt": "...",
  "max_length": 8192,
  "temperature": 0.7,
  "stream": true,
  "stop": ["\\nUser:"]
}
```

`max_tokens` and `temperature` could override the defaults. `stop` was used
only when it was a list; otherwise the default `['\\nUser:']` was used.

### Prompt construction

If both `metadata["chat_template"]` and `metadata["chat_messages"]` were
present, the backend rendered the messages into one prompt using the shared
Jinja renderer. It also accepted:

- `metadata["add_generation_prompt"]`, defaulting to true;
- `metadata["chat_template_context"]`, when it was a dictionary.

Without those values, it sent `request.prompt_text` unchanged.

### Streaming and parsing

The backend iterated streamed response lines and extracted text from:

- a top-level `token` string;
- `results[0].text`;
- a top-level `content` string.

Non-JSON lines were treated as raw text. Optional `on_event` callbacks received
provider, raw event, text, and parsed statistics.

### Cancellation

The backend closed its streaming HTTP response when `stop_event` was set. Its
public `stop(prompt_id)` method instead called:

```text
POST <base_url>/api/extra/abort
```

with `{"id": prompt_id}`. This was the only backend-specific remote abort
operation in the original module.

## 11. Chat-template renderer

`templates/chat_template.py` exposed `render_chat_template()` and configured a
Jinja environment with:

- no loader, so templates were supplied as text;
- autoescape disabled;
- whitespace trimming enabled;
- `StrictUndefined`, so missing variables failed rather than silently becoming
  empty strings.

The required inputs were a non-empty template string and a list of messages.
The renderer always provided `messages` and `add_generation_prompt` to the
template. A dictionary in `extra_context` could add or override template
variables.

Jinja `TemplateError` exceptions were wrapped as `ExecutionError` with the
template failure included in the message.

## 12. Public import shape

The original package’s public usage pattern was modality-oriented. The
text-to-text package re-exported `execute`, and backend consumers imported
classes from:

```python
from apmatia.modules.ysparr.modalities.text2text.executor import execute
from apmatia.modules.ysparr.modalities.text2text.backends import (
    KoboldCppBackend,
    OpenAICompatibleBackend,
)
```

The `core` package supplied the types, configuration facade, and exceptions.
There was no Apmatia HTTP route for this original subsystem and no dependency
on standalone Ysparr’s API.

## 13. Original tests and verification surface

The original source was covered by focused tests including:

- `/home/nick/ServerData/repos/apmatia/tests/unit/test_openai_compatible_backend.py`
  - model and endpoint payload construction;
  - chat streaming and completion fallback;
  - Docker loopback URL handling;
  - stop behavior and event extraction;
  - request parameter filtering.
- `/home/nick/ServerData/repos/apmatia/tests/unit/test_koboldcpp_chat_template.py`
  - template rendering;
  - prompt resolution;
  - invalid template and message handling.

The tests exercised the embedded backend contracts directly. They did not
represent a standalone Ysparr service, an authenticated external adapter, a
durable job manager, or an Open WebUI-compatible catalog.

## 14. Ownership and coupling inventory

The original module combined responsibilities that are now intended to belong
to different boundaries:

| Responsibility | Original owner | Historical mechanism |
|---|---|---|
| Request schema | Apmatia Ysparr module | `PromptRequest` |
| Model name selection | Apmatia Ysparr module | backend constructor/configuration |
| Provider transport | Apmatia Ysparr module | synchronous `requests` adapters |
| Stream parsing | Apmatia Ysparr module | backend-specific line parsers |
| Cancellation | Apmatia process/backend | `Event`, response close, KoboldCpp abort |
| Output persistence | Apmatia Ysparr module | `TextFileStorage` |
| Retention | Apmatia Ysparr module | file count and age cleanup |
| Prompt templating | Apmatia Ysparr module | Jinja/KoboldCpp renderer |
| Configuration storage | Apmatia core | shared nested app config |
| Module discovery | Apmatia core | registry metadata |

The most important coupling points for migration were therefore:

1. Callers that constructed `PromptRequest` directly.
2. Callers that imported `text2text.execute`.
3. Callers that instantiated either concrete backend.
4. Configuration values under `text2text.*`.
5. Code relying on output files named `<prompt_id>.txt`.
6. Code relying on `stop_event` or backend `.stop()` semantics.
7. Code relying on `metadata["chat_messages"]`, chat templates, or
   `metadata["on_event"]` callbacks.

The repository’s historical direct backend tests confirm that the concrete
backend classes were still treated as importable contracts even though the
registry hook itself was small.

## 15. Differences from standalone Ysparr

The original embedded module should not be confused with the standalone
`/home/nick/ServerData/repos/Ysparr` service:

| Original embedded Apmatia module | Standalone Ysparr service |
|---|---|
| In-process Python calls | HTTP service boundary |
| Caller supplies backend and storage | Service owns upstream adapter and job manager |
| Synchronous generator | Durable async job execution |
| Plain text files | SQLite job persistence and event records |
| Local `Event` cancellation | Job cancellation and disconnect durability |
| Backend-specific Apmatia config | Service-level environment configuration |
| No model catalog contract | OpenAI-compatible `/v1/models` route |
| No external extension contract | Optional versioned external extensions |
| Result points to a file | Result is an API response/job record |

This difference is the central reason the original package was considered an
embedded competing subsystem rather than a thin adapter.

## 16. Migration hazards preserved by this record

The following behaviors should be treated as compatibility questions before
removing the historical implementation:

- `prompt_id` doubled as both an execution identifier and a filename stem.
- File persistence could be disabled per request.
- Existing output could be appended to with `append_existing`.
- Retention could delete files globally or by age during finalization.
- Chat-capable providers could fall back from `/v1/chat/completions` to
  `/v1/completions`.
- Multimodal messages intentionally prevented that fallback.
- KoboldCpp accepted rendered Jinja chat templates and used a provider-specific
  abort endpoint.
- Callback consumers could depend on raw provider events and extracted text.
- Stop behavior was cooperative and differed between executor, HTTP response
  closure, and KoboldCpp remote abort.
- The default OpenAI-compatible model was `gpt-4o-mini` when no configured
  model name was supplied.
- Docker loopback rewriting changed how local model servers were reached from
  the Apmatia container.

These are historical facts about the embedded implementation, not guarantees
that the external Ysparr contract should reproduce every internal detail.

## 17. Intended disposition

The original module’s execution, provider, modality, and local persistence
responsibilities are candidates for retirement from Apmatia. The surviving
Apmatia-owned responsibilities are narrower:

- maintain agent records and published-agent eligibility;
- expose the Apmatia-owned extension catalog and resolver;
- provide a small HTTP client for Apmatia-initiated requests to standalone
  Ysparr;
- preserve explicit compatibility for named and legacy agent selectors during
  migration.

The standalone Ysparr service remains responsible for relay execution,
upstream transport, streaming, durable jobs, cancellation, and service-level
failure handling.
