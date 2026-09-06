# Ysparr v1 Architecture Blueprint

## 1. Purpose

**Ysparr Sends Prompts And Receives Responses.**

Ysparr is a reusable conversational relay that sits between OpenAI-compatible chat clients and AI backends.

Its primary job is simple:

> Accept a request, keep responsibility for it even if the client disappears, allow applications to add behavior around the request/response path, send the request upstream, retain the result long enough for recovery, and return responses to clients when possible.

Ysparr must be useful by itself. A user should be able to configure Open WebUI to point at Ysparr, configure Ysparr to use LiteLLM, and immediately gain durable long-running requests without installing Apmatia, TroubleShell, or another Ysparr-aware application.

## 2. Product Boundary

Ysparr owns:

- an OpenAI-compatible inbound API
- durable request execution
- upstream transport through a replaceable provider adapter
- streaming while the client remains connected
- persistence required for reconnect/recovery
- request/response lifecycle state
- extension hooks that allow other applications to add behavior
- reconciliation of responses completed while a client was disconnected
- service operation and a small administrative CLI

Ysparr does **not** own:

- chat UI
- agent definitions
- tool semantics or agent policy
- terminal UI
- model hosting
- provider-specific user experience
- permanent conversation history
- a model manager

Those belong elsewhere.

## 3. First Consumers

### Standalone

```text
Open WebUI
    |
    v
  Ysparr
    |
    v
 LiteLLM
    |
    v
   AI
```

Value: long CPU-bound generations survive intermittent client connectivity.

### TroubleShell

```text
Open WebUI
    |
    v
  Ysparr <----> TroubleShell integration <----> TroubleShell GTK/VTE
    |
    v
 LiteLLM
    |
    v
   AI
```

TroubleShell proper owns only its GTK/VTE experience and terminal actions. The integration contributes terminal context to requests and consumes model replies to derive command suggestions.

### Apmatia

```text
Open WebUI
    |
    v
  Ysparr <----> Apmatia integration
    |
    v
 LiteLLM
    |
    v
   AI
```

Apmatia contributes agent identity, prompts, tools, policy, memory, and tool-loop behavior. Ysparr remains unaware of what an agent means.

## 4. Architectural Principles

### 4.1 Transparent by default

A standard OpenAI-compatible client should need no modification beyond pointing its API connection at Ysparr.

### 4.2 Durable upstream ownership

Once Ysparr accepts a request, the browser or phone is not responsible for keeping the model request alive. Ysparr owns the upstream lifecycle until completion, failure, or cancellation.

### 4.3 Replaceable upstream adapter

LiteLLM is the v1 upstream implementation, not a permanent hard dependency in the architecture. All provider communication goes through an adapter boundary.

```text
Ysparr Core -> UpstreamAdapter -> LiteLLM
```

A future adapter can replace LiteLLM without changing request lifecycle, persistence, extensions, or client-facing behavior.

### 4.4 Applications add behavior; Ysparr does not absorb applications

Ysparr provides extension points. TroubleShell and Apmatia behavior must remain outside Ysparr core.

### 4.5 Retention is durability, not permanent memory

Ysparr stores enough data to recover and reconcile interrupted exchanges. It is not intended to become the canonical chat-history database.

### 4.6 Prefer boring interfaces

OpenAI compatibility externally. Small typed internal contracts. Minimal magic.

## 5. Request Lifecycle

Recommended states:

```text
accepted
   |
   v
queued
   |
   v
running
   |
   +-------> failed
   |
   +-------> cancelled
   |
   v
completed
   |
   +-------> delivered
   |
   +-------> orphaned
   |
   v
expired
```

Definitions:

- **accepted**: request validated and durably recorded
- **queued**: waiting for upstream execution
- **running**: upstream request is active
- **completed**: full response has been retained
- **delivered**: connected client received completion normally
- **orphaned**: completion exists but original client disconnected before delivery
- **failed**: terminal execution failure retained for inspection
- **cancelled**: explicitly cancelled
- **expired**: retention policy removed recoverable payload

The exact storage representation may collapse some of these into fields rather than literal states; the semantics are what matter.

## 6. OpenAI-Compatible API

For v1, prioritize the subset required by Open WebUI and similar clients.

Initial endpoints:

```text
GET  /v1/models
POST /v1/chat/completions
```

Both streaming and non-streaming chat completions should be supported.

Ysparr should preserve the OpenAI-compatible request and response shape wherever possible. Provider-specific transformation belongs in the upstream adapter.

A Ysparr-specific administrative API may coexist under a separate namespace:

```text
GET    /ysparr/v1/jobs
GET    /ysparr/v1/jobs/{id}
DELETE /ysparr/v1/jobs/{id}
GET    /ysparr/v1/status
```

These endpoints are for Ysparr administration and integrations, not requirements for generic chat clients.

## 7. Durable Client Disconnects

### 7.1 Connected case

When the client stays connected:

1. client submits chat completion
2. Ysparr records the request
3. extensions process outbound context
4. Ysparr sends upstream through the adapter
5. stream chunks are retained and forwarded
6. extensions observe/process the completed response
7. Ysparr marks delivery complete

The user experiences a normal streaming chat.

### 7.2 Disconnected case

If the client disconnects after submission:

1. Ysparr does **not** cancel upstream work solely because the downstream HTTP connection disappeared
2. Ysparr continues receiving the upstream response
3. response data is retained according to policy
4. completion is marked orphaned
5. a later request may reconcile the missing assistant turn

This is a core Ysparr feature.

## 8. Conversation Reconciliation Without Client Modification

Generic OpenAI-compatible clients cannot be commanded to retroactively insert an assistant message into an old point in their transcript. Ysparr therefore repairs continuity on the next request.

Example:

Original request seen by Ysparr:

```text
system
user: Why is llama.cpp consuming so much RAM?
```

The client disconnects. Ysparr later stores:

```text
assistant: The largest contributor appears to be...
```

When the client reconnects and sends:

```text
system
user: Why is llama.cpp consuming so much RAM?
user: What should I change first?
```

Ysparr recognizes that a stored orphan belongs between the two user turns and constructs the upstream conversation as:

```text
system
user: Why is llama.cpp consuming so much RAM?
assistant: The largest contributor appears to be...
user: What should I change first?
```

Thus the model receives correct history even though the client never saw the missing response.

For the client-facing response, Ysparr may prepend the recovered response before the newly generated answer, clearly separated, so a completely unmodified client catches up.

### 8.1 Conversation lineage

Do not identify conversations only by system prompt or context prefix.

Ysparr should derive a canonical lineage fingerprint from the message sequence it receives. A practical first implementation can hash canonicalized message content and roles up through the submitted user turn.

Store enough lineage metadata to determine whether a later request is a continuation of an earlier orphaned exchange.

Recommended policy:

```text
exact/unambiguous lineage continuation -> reconcile automatically
ambiguous continuation                 -> do not guess
no continuation                        -> keep orphan until retention expiry
```

Future clients or integrations may provide explicit conversation IDs, but they are optional enhancements rather than a v1 requirement.

## 9. Persistence and Retention

SQLite is the recommended v1 default because Ysparr needs durable state but not a distributed database.

Store at minimum:

- job ID
- timestamps
- lifecycle state
- model/upstream metadata
- canonical request messages required for lineage
- retained response content/events
- delivery/orphan status
- extension metadata where explicitly namespaced
- failure information

Suggested configuration model:

```yaml
retention:
  completed: 7d
  failed: 24h
  orphaned: 30d
  max_storage: 2GiB
```

Exact defaults may change after testing.

Secrets should never be copied into retained request metadata unless absolutely required.

## 10. Extension Architecture

Extensions allow applications to add behavior without moving their code into Ysparr core.

The extension system must support both directions of the conversation.

Conceptual lifecycle:

```text
client request
    |
    v
request_received
    |
    v
request_enrichment
    |
    v
before_upstream
    |
    v
upstream execution
    |
    v
response/chunk observation
    |
    v
after_upstream
    |
    v
before_client_response
    |
    v
client
```

Do not freeze exact hook names until implementation proves the smallest useful contract.

### 10.1 Extension capabilities

An extension may need to:

- inspect request metadata
- add or alter messages before upstream execution
- add tool schemas or other supported request fields
- observe streaming chunks
- inspect complete responses
- request another upstream turn (needed later for agent/tool loops)
- emit side effects to another application
- attach namespaced metadata to a Ysparr job

### 10.2 Out-of-process future

The first implementation may use in-process Python extensions for speed of development, but the contract must not require that forever.

Design extension data structures so the same operations can later cross a Unix socket, local HTTP endpoint, or another IPC mechanism.

Ysparr core must not import TroubleShell or Apmatia packages.

## 11. Upstream Adapter

Define a small adapter interface rather than calling LiteLLM throughout the codebase.

Conceptually:

```python
class UpstreamAdapter:
    async def list_models(...): ...
    async def complete(...): ...
    async def stream(...): ...
    async def cancel(...): ...
```

The v1 implementation is `LiteLLMAdapter`.

Ysparr should pass OpenAI-compatible structures through with as little transformation as practical.

## 12. Service and CLI

Ysparr should be one installable application with a long-running service mode and a small administrative CLI.

Recommended interface:

```text
ysparr serve
ysparr status
ysparr jobs
ysparr job show <id>
ysparr job cancel <id>
ysparr config show
ysparr config check
```

`ysparr serve` starts the gateway/service. The other commands communicate with the running service or inspect local state safely.

No interactive TUI is required for v1.

## 13. Suggested Package Layout

```text
src/ysparr/
├── cli.py
├── config.py
├── server/
│   ├── app.py
│   ├── openai_routes.py
│   └── admin_routes.py
├── core/
│   ├── jobs.py
│   ├── lifecycle.py
│   ├── lineage.py
│   ├── reconciliation.py
│   └── types.py
├── persistence/
│   ├── store.py
│   └── sqlite.py
├── upstream/
│   ├── base.py
│   └── litellm.py
├── extensions/
│   ├── base.py
│   ├── manager.py
│   └── context.py
└── streaming/
    ├── relay.py
    └── retention.py
```

This is a blueprint, not a mandate. Prefer fewer files if implementation proves a simpler structure sufficient.

## 14. Migration From Apmatia Ysparr

The existing Apmatia Ysparr is useful seed material, not the architecture to preserve wholesale.

### KEEP / ADAPT

- the name and core send/receive concept
- backend-agnostic intent
- shared request/result type ideas
- streaming-oriented execution concepts
- separation of generic core from modality/provider details

### REPLACE

- Apmatia shared configuration dependency -> standalone Ysparr configuration
- direct KoboldCpp/OpenAI backend selection -> replaceable upstream adapter, LiteLLM first
- import-oriented library API -> service + OpenAI-compatible API + CLI
- per-modality backend ownership for text chat -> central durable relay path
- plain text-file persistence -> structured durable job store

### DROP FROM YSPARR CORE

- Apmatia module registration and manifests
- Apmatia-specific imports
- assumptions that callers invoke modality functions directly
- provider-specific logic spread throughout modality code

### DEFER

The old Ysparr envisioned many modalities: text-to-image, text-to-speech, image-to-image, mesh generation, and more. Do not build them into v1 merely because the old module planned for them.

The v1 architecture should avoid preventing future modalities, but the release target is durable conversational text middleware.

## 15. Extraction Strategy

Do **not** remove Ysparr from Apmatia first.

Migration sequence:

1. freeze the embedded Apmatia Ysparr except for necessary maintenance
2. use its useful code and ideas as seed material for standalone Ysparr
3. make standalone Ysparr independently usable with Open WebUI + LiteLLM
4. implement and validate durability/reconciliation
5. use TroubleShell as the first external extension/integration consumer
6. migrate Apmatia to consume standalone Ysparr
7. only after Apmatia works through standalone Ysparr, remove the embedded module

This prevents simultaneous redesign and extraction from destabilizing Apmatia.

## 16. v1 Release Scope

Ysparr v1.0 is complete when:

1. Open WebUI can be configured to use Ysparr as an OpenAI-compatible endpoint with no code/plugin modification.
2. Ysparr can list available models from its configured upstream.
3. normal streaming chat works through Ysparr.
4. disconnecting the client does not terminate an accepted upstream generation.
5. Ysparr retains the completed response according to configurable policy.
6. a subsequent continuation can reconcile an unambiguously orphaned assistant turn into upstream context.
7. the client is informed of recovered output on the next response without requiring custom client support.
8. LiteLLM is isolated behind a replaceable adapter.
9. an extension can inspect/enrich requests and observe completed responses.
10. administrative CLI commands can inspect job state and cancel active work.
11. automated tests cover request lifecycle, disconnect durability, reconciliation, retention, streaming, and extension ordering.

## 17. Explicit Non-Goals for v1

- agent implementation
- tool implementation
- TroubleShell UI behavior
- permanent chat-history management
- distributed clustering
- multi-node queues
- elaborate plugin marketplace
- every generative modality
- custom Open WebUI plugin
- GUI/TUI administration

If a feature is not required for durable transparent conversation relay or reusable request/response extension behavior, it should probably wait.

## 18. Implementation Phases

### Phase 0 — Skeleton

- packaging
- configuration
- CLI entry point
- service startup
- health/status endpoint

### Phase 1 — Transparent relay

- `/v1/models`
- `/v1/chat/completions`
- LiteLLM adapter
- streaming and non-streaming pass-through
- error propagation

Success criterion: Open WebUI chats normally through Ysparr.

### Phase 2 — Durable jobs

- SQLite store
- job lifecycle
- upstream task decoupled from downstream HTTP connection
- retained response stream/content
- status/jobs CLI

Success criterion: close the client during a long generation and Ysparr still completes it.

### Phase 3 — Reconciliation

- canonical message normalization
- lineage fingerprinting
- orphan matching
- upstream history repair
- recovered-response presentation on the next client reply
- ambiguity safeguards

Success criterion: reconnect later, continue the same conversation, and both user and model regain the missing turn.

### Phase 4 — Extension contract

- minimal request enrichment hook
- response observation hook
- namespaced extension metadata
- ordering/error policy
- extension test fixture/example

Success criterion: a dummy extension can inject context and observe a response without changing Ysparr core.

### Phase 5 — TroubleShell proof

Outside the Ysparr core repository if appropriate:

- TroubleShell integration obtains terminal context
- request enrichment attaches relevant terminal context
- response observer extracts command/code suggestions
- suggestions are sent to TroubleShell GTK/VTE UI

Success criterion: Open WebUI remains unmodified while TroubleShell gains terminal-aware AI behavior.

### Phase 6 — Apmatia migration

- standalone Ysparr becomes Apmatia's conversational transport
- Apmatia implements agent/tool behavior through the extension boundary
- embedded Ysparr is removed only after parity is proven

## 19. Design Rule

When choosing whether functionality belongs in Ysparr, ask:

> Is this functionality about reliably passing a conversation between a client and an AI, retaining it across interruption, or allowing another application to participate in that passage?

If yes, it may belong in Ysparr.

If it is about what a specific application *does* with the conversation, it belongs in that application or its Ysparr integration.
