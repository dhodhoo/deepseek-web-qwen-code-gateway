# Public API Contract

## Scope

The first compatibility target is the OpenAI-style **Chat Completions API** sufficient for Qwen Code.

Primary prefix:

```text
/v1
```

## Implementation status

Last synchronized: M7 (ADR-028), on top of M6 + post-M6 hotfix (ADR-024..027). See also docs/PROGRESS.md and docs/DECISIONS.md (ADR-017..028).

- `GET /health` — implemented (M2). Unauthenticated by design; exposes no secrets.
- `GET /v1/models` — implemented (M2). One gateway alias (`GATEWAY_MODEL_ID`, default `deepseek-web`).
- `POST /v1/chat/completions` — implemented (M2 non-streaming; M3 OpenAI SSE streaming; M6 prompt-emulated tool calling) for plain chat with `system` / `user` / `assistant` text messages:
  - `stream: true` → implemented (M3): `chat.completion.chunk` SSE lines, role on the first chunk, incremental `content`, terminal chunk with mapped `finish_reason` (`length` passes through, else `stop`; `tool_calls` when a tool call was emitted, M6), terminated by `data: [DONE]`. No usage chunk is emitted (no upstream token counts; clients must tolerate absence).
  - Streaming errors: failures BEFORE the first byte answer real HTTP statuses (4xx/5xx, OpenAI error body); failures MID-stream emit `data: {"error": {...}}` and close WITHOUT `[DONE]`. Since M7 (ADR-028), tool-ENABLED turns can only fail pre-response — the backend work completes fully before the first synthesized byte, so the mid-stream error envelope is unreachable on that path; it remains the behavior for tool-DISABLED streaming.
  - `tools` / `tool_choice` → implemented as prompt-emulated tool calling (M6, ADR-023, supersedes the M5 "accepted and ignored" behavior; hardened in M7, ADR-028). Incoming `tools[]` are normalized leniently (malformed entries silently skipped; duplicates first-wins). When at least one valid tool remains and `tool_choice != "none"`, a deterministic `[available tools]` instruction block is appended to the compiled prompt and the model's output is parsed for the control envelope (`docs/TOOL_CALLING_PROTOCOL.md`). The rendered block is COMPACTED for the upstream prompt budget (ADR-024): descriptions reduce to their first line (≤150 chars) and schema `description` keys are stripped — validation still uses the full schema. A valid envelope becomes a structured OpenAI `tool_calls` output with a gateway-minted `call_dsqg_<hex>` id; invalid/truncated envelopes NEVER surface partially (see buffering below), and no `tool_calls` are ever fabricated. `tool_choice: "required"` strengthens the instruction ("You MUST request exactly one tool call now"); `"none"` fully disables tools. One tool call per turn (parallel calls deferred); REPEATED tool-result/model cycles are fully supported (M7) — tool ids round-trip verbatim through re-sent history. Tool-ENABLED turns are BUFFERED (M7): the whole turn is parsed before any response byte; if no valid call emerged, ONE bounded repair retry runs (static hint listing the valid tool names; never echoes model output) before the honest plain-text fallback — at most two backend calls per turn. Repair trigger (M7/ADR-028, widened by ADR-029): the turn was `tool_choice: "required"`, OR the parser saw a malformed/truncated envelope attempt, OR the turn is PRE-LOOP — the request history holds no assistant tool call yet, so envelope-less plain text is the prose-simulated-tool-use failure mode (the instructions also forbid simulating tool execution in prose). MID-loop text answers (history already carries tool calls) are presumed final and never repaired. When tools are disabled or none remain valid, the M5 behavior stands: plain text, nothing echoed.
  - `role=tool` and assistant `tool_calls` history → ACCEPTED AND COMPILED (M6, ADR-023): `role=tool` requires a non-empty `tool_call_id` and compiles to a `[tool result]` block; assistant `tool_calls` compile to `[assistant tool call]` blocks with arguments normalized to compact JSON both directions (structural round-trip equality). Assistant null-content messages are valid only with `tool_calls`; null content without tool calls is still `400 UNSUPPORTED_MESSAGE`, as are malformed tool-call entries (missing/empty id or name, non-JSON arguments). M7 (ADR-028) adds LENIENT history validation on top: orphan tool results (a `tool_call_id` matching no assistant call in the history) never reject the request — the history compiles as-is (tool name rendered `unknown`, ids verbatim) and the gateway logs a minimal warning. Tool-call ids persist across turns via an index derived per request from the request's own history (no server-side registry).
  - Unknown request fields (sampling knobs, `stream_options`, vendor extras) are accepted and ignored (lenient parsing, `extra="allow"`).
  - Unknown `model` → `404 model_not_found`; empty/missing `messages` or `model` → `422`.
- Authentication (M2): `Authorization: Bearer <DEEPSEEK_GATEWAY_API_KEY>` on `/v1/*`. Secure-by-default: unconfigured key → `503 GATEWAY_API_KEY_NOT_CONFIGURED` unless `GATEWAY_ALLOW_NO_AUTH=1` (ADR-017).
- Error envelope (M2): `{"error": {"message", "type", "code"}}`; `BackendFailure` categories map per the suggested HTTP table with `code` = category value (`app/error_mapping.py`).
- Conversation continuity (M4, ADR-020): resolved from the request's own message history — no conversation header exists or is required. A request whose history STRICTLY extends a stored canonical history continues that conversation: the gateway reuses the backend session, sends only the new trailing messages upstream, and threads `parent_message_id` (serialized to DeepSeek Web as the numeric u32 it requires, ADR-025 — live-verified). New, divergent, or duplicate (equal-history) requests start a fresh conversation compiled from the request's full history. Canonical history advances only when a turn completes; failures invalidate the backend link and the next request rebuilds from canonical state. Since M7 (ADR-028) this also applies after any MULTI-ATTEMPT tool turn (a bounded repair happened): the upstream session then contains an orphaned attempt branch, so the link is invalidated after committing the final result and the next request rebuilds from canonical history. State is in-memory only (bounded; lost on restart — continuity self-heals because requests carry their own history).
- Backend call serialization (ADR-027): the DeepSeek Web backend is single-flight — concurrent `/v1/chat/completions` requests are queued at the backend boundary (the vendored client's shared wasmtime PoW solver and parser seam are not thread-safe; racing them crashed the process). Clients see normal responses, just serialized latency.
- Streaming tool-call chunks: implemented (M6, ADR-023) — see "Tool calls in streaming mode" below. Responses serialize with null fields omitted (`exclude_none`), so plain responses keep the exact M2 shape and tool-call responses omit a null `content`.
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

Tool-call example (implemented M6, ADR-023):

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
        "tool_calls": [
          {
            "id": "call_dsqg_57e118c6d48147efb02ad96d72b37f72",
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

`function.arguments` must be a JSON string in the public OpenAI-compatible response. Null fields are omitted (`exclude_none` serialization), so a tool-call message omits `content` entirely. When the model produced text before the tool envelope, `content` carries that text alongside `tool_calls`.

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

Implemented behavior (M6, ADR-023; buffered since M7, ADR-028; repair trigger widened by ADR-029): tool-enabled turns are drained through the envelope parser completely BEFORE any chunk is emitted; the outcome is then re-emitted through the same chunk renderer, so the public shapes below are byte-compatible with M6. Text before a valid envelope renders as normal `content` deltas; the sentinel itself is held back across chunk boundaries until the envelope is decided. A VALID envelope emits exactly two tool-call chunks — an opener (`index: 0`, id, `type: "function"`, name, empty arguments; plus `role` if not yet sent) and one arguments chunk with the full compact JSON — then the terminal chunk with `finish_reason: "tool_calls"` (overriding the backend reason). If no valid call emerged AND the turn was `required`, attempted an envelope (malformed/truncated), or is PRE-loop (no assistant tool call in the history yet — ADR-029), ONE bounded repair retry runs before the honest fallback: the retry's outcome — valid call or plain text — is the ONLY thing rendered. Exactly one tool call per turn; repeated tool-result/model cycles are fully supported (each cycle is one buffered turn).

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
