# Public API Contract

## Scope

The first compatibility target is the OpenAI-style **Chat Completions API** sufficient for Qwen Code.

Primary prefix:

```text
/v1
```

## Implementation status

Last synchronized: M5 (2026-08-14). See also docs/PROGRESS.md and docs/DECISIONS.md (ADR-017..021).

- `GET /health` — implemented (M2). Unauthenticated by design; exposes no secrets.
- `GET /v1/models` — implemented (M2). One gateway alias (`GATEWAY_MODEL_ID`, default `deepseek-web`).
- `POST /v1/chat/completions` — implemented (M2 non-streaming; M3 OpenAI SSE streaming) for plain chat with `system` / `user` / `assistant` text messages:
  - `stream: true` → implemented (M3): `chat.completion.chunk` SSE lines, role on the first chunk, incremental `content`, terminal chunk with mapped `finish_reason` (`length` passes through, else `stop`), terminated by `data: [DONE]`. No usage chunk is emitted (no upstream token counts; clients must tolerate absence).
  - Streaming errors: failures BEFORE the first byte answer real HTTP statuses (4xx/5xx, OpenAI error body); failures MID-stream emit `data: {"error": {...}}` and close WITHOUT `[DONE]`.
  - `tools` / `tool_choice` → ACCEPTED AND IGNORED (M5, ADR-021, partially supersedes the M2-era `TOOLS_NOT_YET_SUPPORTED` 400): responses are plain text, tools are never echoed and no `tool_calls` are fabricated. Qwen Code sends a non-empty `tools[]` on every agent turn, so tolerating it is what makes plain chat usable at all. Structured tool-call output arrives in M6.
  - `role=tool`, assistant `tool_calls`, null-content assistant messages → `400`, `code: UNSUPPORTED_MESSAGE` (until M6; the exact shapes are fixtured in `tests/fixtures/qwen_code_wire/tool_history_turn.json`).
  - Unknown request fields (sampling knobs, `stream_options`, vendor extras) are accepted and ignored (lenient parsing, `extra="allow"`).
  - Unknown `model` → `404 model_not_found`; empty/missing `messages` or `model` → `422`.
- Authentication (M2): `Authorization: Bearer <DEEPSEEK_GATEWAY_API_KEY>` on `/v1/*`. Secure-by-default: unconfigured key → `503 GATEWAY_API_KEY_NOT_CONFIGURED` unless `GATEWAY_ALLOW_NO_AUTH=1` (ADR-017).
- Error envelope (M2): `{"error": {"message", "type", "code"}}`; `BackendFailure` categories map per the suggested HTTP table with `code` = category value (`app/error_mapping.py`).
- Conversation continuity (M4, ADR-020): resolved from the request's own message history — no conversation header exists or is required. A request whose history STRICTLY extends a stored canonical history continues that conversation: the gateway reuses the backend session, sends only the new trailing messages upstream, and threads `parent_message_id`. New, divergent, or duplicate (equal-history) requests start a fresh conversation compiled from the request's full history. Canonical history advances only when a turn completes; failures invalidate the backend link and the next request rebuilds from canonical state. State is in-memory only (bounded; lost on restart — continuity self-heals because requests carry their own history).
- Streaming tool-call chunks: NOT yet implemented (M6).
- Qwen Code wire format (M5, ADR-021): the exact current agent request/history format is documented in docs/UPSTREAM_NOTES.md (source verification, Qwen Code v0.21.11) and covered by fixtures in `tests/fixtures/qwen_code_wire/` plus tests (`test_m5_wire_fixtures.py`, SDK-driven `test_m5_sdk_compat.py`). Wiring steps: docs/QWEN_CODE_INTEGRATION.md.
- Diagnostic capture (M5, ADR-021): opt-in via `GATEWAY_DIAGNOSTICS_DIR`; appends one sanitized record per authenticated `/v1/chat/completions` request (before validation) to `<dir>/requests.jsonl`. The Authorization header VALUE is never written; request bodies are (that is the purpose of the layer). Disabled by default (app/diagnostics.py).

## Authentication

Support a local gateway API key:

```http
Authorization: Bearer <GATEWAY_API_KEY>
```

The gateway API key is not the DeepSeek auth token.

For development, authentication may be configurable, but secure-by-default behavior is preferred.

---

## GET /health

Purpose: process/service health, not model output.

Example:

```json
{
  "ok": true,
  "version": "0.1.0",
  "backend": {
    "type": "deepseek_web",
    "status": "ready"
  }
}
```

Do not expose secrets.

---

## GET /v1/models

Return an OpenAI-compatible list shape.

Example:

```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-web",
      "object": "model",
      "created": 0,
      "owned_by": "local"
    }
  ]
}
```

Model IDs are gateway aliases, not necessarily official upstream model IDs.

---

## POST /v1/chat/completions

### Minimum accepted request fields

```json
{
  "model": "deepseek-web",
  "messages": [],
  "stream": true,
  "tools": [],
  "tool_choice": "auto"
}
```

Support progressively:

### Required for core

- `model`
- `messages`
- `stream`
- `tools`
- `tool_choice`

### Accept but initially may ignore/map with documented behavior

- `temperature`
- `top_p`
- `max_tokens` / client equivalent
- `stop`
- `frequency_penalty`
- `presence_penalty`
- `seed`
- `user`

Never silently pretend an unsupported parameter is enforced. Either:

- map it,
- explicitly ignore it with internal trace/debug metadata,
- or reject it if semantic correctness requires rejection.

## Message roles

Core support:

```text
system
user
assistant
tool
```

Assistant messages may contain:

- normal `content`,
- `tool_calls`.

Tool messages should contain:

- `tool_call_id`,
- `content`.

## Non-stream response

Text example:

```json
{
  "id": "chatcmpl_local_x",
  "object": "chat.completion",
  "created": 0,
  "model": "deepseek-web",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "..."
      },
      "finish_reason": "stop"
    }
  ]
}
```

Tool-call example:

```json
{
  "id": "chatcmpl_local_x",
  "object": "chat.completion",
  "created": 0,
  "model": "deepseek-web",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_local_x",
            "type": "function",
            "function": {
              "name": "read",
              "arguments": "{\"filePath\":\"src/main.py\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

`function.arguments` must be a JSON string in the public OpenAI-compatible response.

## Streaming response

Content type:

```http
text/event-stream
```

Normal text should be sent as OpenAI-compatible chat completion chunks.

Terminate with:

```text
data: [DONE]
```

Do not leak the private backend's raw SSE framing.

### Tool calls in streaming mode

Reliability is more important than maximum incremental streaming.

For v1:

- stream normal prose promptly,
- when output may be a tool-control envelope, buffer the candidate,
- validate the envelope,
- emit a valid OpenAI tool-call sequence,
- use `finish_reason: "tool_calls"`.

Do not display internal control syntax to Qwen Code as normal assistant prose.

## Conversation identity

OpenAI Chat Completions does not provide a universal server conversation ID contract.

The gateway may infer conversation continuity from incoming message history.

If an optional internal header is introduced, it must not be required for Qwen Code compatibility.

Prefer correctness from the request's canonical message history.

Implemented in M4 (ADR-020): continuity is inferred exclusively from the
incoming canonical message history (longest strict prefix match against the
local canonical store). No header exists, and conversation identity is
internal gateway state only — it never appears in responses.

## Error responses

Use OpenAI-like JSON where practical:

```json
{
  "error": {
    "message": "Upstream authentication failed",
    "type": "upstream_authentication_error",
    "code": "AUTH_INVALID"
  }
}
```

Suggested HTTP mappings:

```text
400 malformed client request
401 invalid gateway API key
422 validation error where appropriate
429 no available account / upstream rate limited
502 upstream protocol/server failure
503 backend temporarily unavailable / Cloudflare unavailable
500 internal bug
```

Do not return the DeepSeek token or raw sensitive upstream response content in errors.

## Optional future endpoints

Not part of core acceptance:

```text
/admin/accounts
/admin/health
/admin/sessions
/admin/metrics
```

Do not let these delay core Qwen Code compatibility.
