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

Define an internal backend interface conceptually similar to:

```python
class LLMBackend(Protocol):
    def create_session(...) -> BackendSession: ...
    def stream_turn(...) -> Iterator[BackendEvent]: ...
    def health_check(...) -> BackendHealth: ...
```

Exact signatures may evolve.

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

## Later admin UI

Admin UI is intentionally outside the core milestones.

When added it may expose:
- account health,
- add/disable account,
- cooldown state,
- gateway status,
- request metadata,
- session count,
- sanitized logs,
- settings.

It must never reveal full stored credentials after save.
