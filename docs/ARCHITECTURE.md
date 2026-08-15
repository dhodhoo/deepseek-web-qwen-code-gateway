# Architecture

## High-level design

```text
┌───────────────────────────────────────┐
│               Qwen Code                │
│                                       │
│ read/edit/write/bash/grep/...         │
└──────────────────┬────────────────────┘
                   │
                   │ OpenAI-compatible HTTP/SSE
                   ▼
┌───────────────────────────────────────┐
│         DeepSeek Agent Gateway        │
│                                       │
│ API layer                             │
│ OpenAI schema compatibility           │
│ Message compiler                      │
│ Tool protocol compiler/parser         │
│ Conversation manager                  │
│ Stream translator                     │
│ Error normalization                   │
│ Account selection (later)             │
│ Observability                         │
└──────────────────┬────────────────────┘
                   │
                   │ backend interface
                   ▼
┌───────────────────────────────────────┐
│          DeepSeekWebBackend           │
│                                       │
│ upstream-specific request logic       │
│ PoW integration                       │
│ cookie / CF integration               │
│ SSE normalization                     │
└──────────────────┬────────────────────┘
                   │
                   ▼
          DeepSeek private Web API
```

## Design principle: anti-corruption layer

The rest of the application must not understand private DeepSeek endpoint details.

The internal backend interface is concrete since M1 in `app/backends/base.py`
(see docs/DECISIONS.md ADR-014):

```python
class LLMBackend(ABC):
    @property
    def backend_type(self) -> str: ...          # class attribute suffices
    def health_check(self) -> BackendHealth: ...
    def create_session(self) -> BackendSession: ...
    def stream_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        parent_message_id: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
    ) -> Iterator[BackendEvent]: ...
```

`BackendSession(session_id)` and `BackendHealth(backend_type, ready, details)`
are frozen value types in the same module. Backends raise `BackendFailure`
(normalized taxonomy) for all failures. Backend-specific extras (e.g.
`DeepSeekWebBackend`'s `raw_sink` probe capture) are not part of the
contract. `app/config.py::build_backend` constructs the configured backend
(ADR-015); an AST-based test enforces that nothing outside
`app/backends/deepseek_web` imports the vendored `dsk` namespace (ADR-016).

The important requirement is that upstream events become stable internal types before reaching API/tool logic.

## Internal event model

Suggested normalized backend events:

```text
TextDelta(text)
ReasoningDelta(text)        # optional internal/vendor data
MessageStarted(...)
MessageFinished(...)
BackendMessageId(id)
BackendError(kind, retryable, message)
```

Do not make the OpenAI SSE schema the internal backend schema.

This keeps the backend replaceable.

## Public API boundary

The API layer handles:

- request authentication,
- OpenAI request validation,
- model alias resolution,
- conversation resolution,
- error mapping,
- streaming response framing.

It should not construct DeepSeek private payloads directly.

## Message compiler

The upstream may accept only a prompt string.

Create a deterministic compiler from normalized OpenAI messages into backend input.

The compiler must understand:

- `system`
- `user`
- `assistant`
- `assistant` with tool calls
- `tool`

The compiler must not rely on lossy ad-hoc concatenation scattered across routes.

## Conversation manager

Maintain local canonical state including at least:

```text
conversation_id
backend_type
backend_account_id
backend_session_id
backend_parent_message_id (if useful/current upstream supports it)
created_at
updated_at
status
normalized message history or reconstructable representation
```

Important:

- A backend remote session is an optimization/state link, not the sole source of truth.
- Later failover should be able to rebuild a remote session from canonical state.

## Sticky account behavior

When multi-account exists, a conversation should normally remain on the account that created its backend session.

Do not round-robin every turn.

## Blocking upstream integration

If current `deepseek4free` calls remain synchronous/blocking, do not block the FastAPI event loop.

Use an appropriate thread/worker bridge and support client disconnect/cancellation as cleanly as practical.

Document any limitations.

## Streaming layers

There are three independent formats:

1. DeepSeek upstream stream
2. Internal normalized backend events
3. OpenAI-compatible SSE output

Never let raw upstream stream bytes leak directly through the public endpoint.

## Tool calling layers

Tool emulation has three pieces:

```text
OpenAI tools[] definitions
      ↓
ToolPromptCompiler
      ↓
DeepSeek control instruction
      ↓
model output
      ↓
ToolEnvelopeParser
      ↓
canonical ToolCall
      ↓
OpenAI assistant.tool_calls
```

See `TOOL_CALLING_PROTOCOL.md`.

## Storage

Initial storage: SQLite.

Potential tables:

### accounts

- id
- label
- encrypted_auth_token
- enabled
- health_status
- cooldown_until
- consecutive_failures
- last_used_at
- created_at
- updated_at

### conversations

- id
- backend
- account_id
- backend_session_id
- backend_parent_message_id
- status
- created_at
- updated_at

### messages

Only if persistent canonical history is required.

Be privacy-conscious. Avoid storing full content by default unless necessary for failover. An alternative is bounded in-memory state for v1 with optional persistence. Record the final decision in `DECISIONS.md`.

### request_metrics

Store metadata, not raw content:

- request id
- conversation id
- account id
- latency
- outcome
- retry count
- tool call count
- timestamps

## Error taxonomy

Normalize upstream failures into application categories:

```text
AUTH_INVALID
RATE_LIMITED
CLOUDFLARE_BLOCKED
UPSTREAM_NETWORK
UPSTREAM_5XX
UPSTREAM_PROTOCOL
CLIENT_BAD_REQUEST
INTERNAL
```

Every category should indicate whether retry/failover is appropriate.

## Admin UI (delivered M12, ADR-039)

Admin UI was intentionally outside the core milestones; M12 delivered it as ONE
self-contained HTML page at `GET /admin` (inline CSS + vanilla JS, no external
assets, no build step) acting as a pure stateless client of additive `/admin/*`
JSON endpoints (`/admin/summary`, `/admin/sessions`, `/admin/settings`, plus
the pre-existing `/admin/metrics` and `/admin/accounts`, and
`POST /admin/accounts/{id}/disable|enable|reset`).

Of the original wish list:

- account health — delivered (dashboard + accounts tab),
- add/disable account — DISABLE/ENABLE/RESET delivered over the M10 router
  seams; runtime ADD/REMOVE stays deferred (would move credentials across the
  admin boundary),
- cooldown state — delivered (accounts tab + `by_state` counts),
- gateway status — delivered (dashboard embeds the exact `/health` payload),
- request metadata — delivered as aggregates (`/admin/metrics` subset),
- session count — delivered (dashboard + `/admin/sessions` metadata rows),
- sanitized logs — NOT delivered (metrics/summary only; no log tail),
- settings — delivered as a read-only effective-config echo.

It never reveals stored credentials — enforced STRUCTURALLY since M10:
`AccountRecord` carries no credential field, and no admin view builder ever
touches a `SecretStr` (secrets surface as presence only: configured/open/unset).
