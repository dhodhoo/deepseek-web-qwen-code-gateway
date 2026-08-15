# Public API Contract

## Scope

The first compatibility target is the OpenAI-style **Chat Completions API** sufficient for Qwen Code.

Primary prefix:

```text
/v1
```

## Implementation status

Last synchronized: M12 (ADR-039), on top of M11 (ADR-038) + M10 (ADR-037) + M9 (ADR-036) + M8 + M7 (ADR-028). See also docs/PROGRESS.md and docs/DECISIONS.md (ADR-017..039).

- `GET /health` — implemented (M2). Unauthenticated by design; exposes no secrets.
- `GET /admin/metrics` — implemented (M9, ADR-036). Unauthenticated like `/health` (local-first gateway; counters/durations only, never secrets). Returns the in-memory `MetricsCollector` snapshot: per-endpoint request counts by status class (`"POST /v1/chat/completions": {"2xx": n, ...}`), request + per-backend-attempt duration summaries (`count`/`sum`/`max`), `backend_attempts` (every retry-wrapped upstream call, session creation included), `backend_failures` by category, `transport_retries`, `session_failovers` (M11, ADR-038 — failover sessions ESTABLISHED on another account, counted before the re-run attempt), `tool_turns`, `tool_repair_retries`, `tool_repair_budget_exhausted`, `uptime_seconds`. Shape pinned by `tests/test_m9_reliability.py`; additive changes only.
- `GET /admin/accounts` — implemented (M10, ADR-037). Unauthenticated like `/admin/metrics` (local-first gateway; the payload is STRUCTURALLY secret-free — credentials live only inside each account's backend instance and no admin surface can serialize them). Read-only; since M12 (ADR-039) the lifecycle mutations live at `POST /admin/accounts/{id}/enable|disable|reset` (below). Returns `{"accounts": [...]}` in config order; per account: `id`, `label`, `enabled`, derived `state` (`disabled` > `invalid` > `cooldown` > `healthy`), `cooldown_remaining_seconds`, `consecutive_failures`, `active_conversations` (derived live from the conversation store), `last_used_at`. Shape pinned by `tests/test_m10_accounts.py`; additive changes only.
- `GET /admin` — implemented (M12, ADR-039). The admin UI: ONE self-contained HTML page (inline CSS + vanilla JS, zero external assets, no build step — usable fully offline) with five tabs (Dashboard / Accounts / Sessions / Metrics / Settings) and 5-second auto-refresh. It is a pure stateless CLIENT of the admin JSON endpoints below plus `/admin/metrics` and `/admin/accounts` — the page holds no state and makes no write beyond the three lifecycle POSTs. Hidden from the OpenAPI schema (`include_in_schema=False`). Unauthenticated like every `/admin/*` surface (local-first; the default 127.0.0.1 bind is the boundary — recorded residual risk).
- `GET /admin/summary` — implemented (M12, ADR-039). Dashboard aggregate: `health` (the EXACT payload `GET /health` produces — both endpoints share one payload closure, so they can never diverge), `backend_type`, `accounts` (`total` + `by_state` counts for healthy/cooldown/invalid/disabled), `conversations`, `active_sessions` (conversations with a live backend link), `uptime_seconds`, and a `metrics` subset (`requests`, `backend_attempts`, `backend_failures`, `transport_retries`, `session_failovers`, `tool_turns`). Unauthenticated; structurally secret-free. Pinned by `tests/test_m12_admin.py`.
- `GET /admin/sessions` — implemented (M12, ADR-039). Returns `{"sessions": [...]}` newest-updated-first; per row: `conversation_id`, `backend_account_id`, `backend_session_id`, `linked` (live backend link), `status`, `message_count`, `tool_call_count` (derived), `created_at`, `updated_at`. BINDING METADATA ONLY by design — message content and tool arguments are never serialized on any admin surface. Unauthenticated. Pinned by `tests/test_m12_admin.py`.
- `GET /admin/settings` — implemented (M12, ADR-039). Read-only echo of the EFFECTIVE configuration: `backend_type`, `model_id`, `host`, `port`, `gateway_auth` (presence only — `configured` / `open` / `unset`, never the key value), `accounts` (`mode` single/multi + `count`), `diagnostics` (`enabled`, `dir`), `reliability` (`max_retries`, `retry_backoff_seconds`, `upstream_timeout_seconds`, `account_cooldown_seconds`). Secrets as PRESENCE ONLY — no `SecretStr` value ever enters the view. No runtime mutation (settings are env-derived; restart applies). Unauthenticated. Pinned by `tests/test_m12_admin.py`.
- `POST /admin/accounts/{account_id}/disable|enable|reset` — implemented (M12, ADR-039). Account lifecycle management as thin projections over the M10 router seams: `disable` = flag off + release ALL conversation links bound to the account (their next requests rebuild elsewhere); `enable` = flag on ONLY — an `invalid` account stays invalid until `reset`; `reset` = enabled + healthy + cleared cooldown + cleared `consecutive_failures`. Each responds `200` with `{"account": <updated masked row — same shape as the /admin/accounts entries>}`. Unknown account id → `404` OpenAI envelope `{"error": {"code": "ACCOUNT_NOT_FOUND", "type": "invalid_request_error", ...}}` for all three actions. Unauthenticated (local-first boundary); in-memory state does not survive restart (the registry rebuilds from config). Runtime account ADD/REMOVE is intentionally NOT exposed (would move credentials across the admin boundary). Pinned by `tests/test_m12_admin.py`.
- `GET /v1/models` — implemented (M2). One gateway alias (`GATEWAY_MODEL_ID`, default `deepseek-web`).
- `POST /v1/chat/completions` — implemented (M2 non-streaming; M3 OpenAI SSE streaming; M6 prompt-emulated tool calling) for plain chat with `system` / `user` / `assistant` text messages:
  - `stream: true` → implemented (M3): `chat.completion.chunk` SSE lines, role on the first chunk, incremental `content`, terminal chunk with mapped `finish_reason` (`length` passes through, else `stop`; `tool_calls` when a tool call was emitted, M6), terminated by `data: [DONE]`. No usage chunk is emitted (no upstream token counts; clients must tolerate absence).
  - Streaming errors: failures BEFORE the first byte answer real HTTP statuses (4xx/5xx, OpenAI error body); failures MID-stream emit `data: {"error": {...}}` and close WITHOUT `[DONE]`. Since M7 (ADR-028), tool-ENABLED turns can only fail pre-response — the backend work completes fully before the first synthesized byte, so the mid-stream error envelope is unreachable on that path; it remains the behavior for tool-DISABLED streaming. Since M9 (ADR-036) this includes TRUNCATION: a turn that ends without a terminal marker is never completed with a fabricated `stop` — pre-byte it is retried within the bounded budget and then answers `502 UPSTREAM_PROTOCOL`; mid-stream it emits the error envelope and closes without `[DONE]`.
  - `tools` / `tool_choice` → implemented as prompt-emulated tool calling (M6, ADR-023, supersedes the M5 "accepted and ignored" behavior; hardened in M7, ADR-028). Incoming `tools[]` are normalized leniently (malformed entries silently skipped; duplicates first-wins). When at least one valid tool remains and `tool_choice != "none"`, a deterministic `[available tools]` instruction block is appended to the compiled prompt and the model's output is parsed for the control envelope (`docs/TOOL_CALLING_PROTOCOL.md`). The rendered block is COMPACTED for the upstream prompt budget (ADR-024): descriptions reduce to their first line (≤150 chars) and schema `description` keys are stripped — validation still uses the full schema. A valid envelope becomes a structured OpenAI `tool_calls` output with a gateway-minted `call_dsqg_<hex>` id; invalid/truncated envelopes NEVER surface partially (see buffering below), and no `tool_calls` are ever fabricated. `tool_choice: "required"` strengthens the instruction ("You MUST request exactly one tool call now"); `"none"` fully disables tools. One tool call per turn (parallel calls deferred); REPEATED tool-result/model cycles are fully supported (M7) — tool ids round-trip verbatim through re-sent history. Tool-ENABLED turns are BUFFERED (M7): the whole turn is parsed before any response byte; if no valid call emerged, ONE bounded repair retry runs (static hint listing the valid tool names; never echoes model output) before the honest plain-text fallback — at most two backend calls per turn. Repair trigger (M7/ADR-028, widened by ADR-029/031/035): EVERY tool-enabled turn that ends without a valid envelope gets the ONE bounded retry — `tool_choice: "required"` turns, malformed/truncated envelope attempts, simulation-marker output (ADR-031, markers extended by ADR-034), pre-loop plain text (ADR-029), and marker-less mid-loop prose (ADR-035, logged `no_envelope`). The old termination guard (mid-loop text presumed final, never repaired) was removed after live falsification; termination is preserved by the budget plus the plain-answer-permitting hint (a genuine final answer pays one extra call, its second-attempt text flushes). When tools are disabled or none remain valid, the M5 behavior stands: plain text, nothing echoed.
  - `role=tool` and assistant `tool_calls` history → ACCEPTED AND COMPILED (M6, ADR-023): `role=tool` requires a non-empty `tool_call_id` and compiles to a `[tool result]` block; assistant `tool_calls` compile to control-ENVELOPE blocks byte-identical to the instructed format (ADR-034; was the internal `[assistant tool call]` blocks before) with arguments normalized to compact JSON both directions (structural round-trip equality). Assistant null-content messages are valid only with `tool_calls`; null content without tool calls is still `400 UNSUPPORTED_MESSAGE`, as are malformed tool-call entries (missing/empty id or name, non-JSON arguments). M7 (ADR-028) adds LENIENT history validation on top: orphan tool results (a `tool_call_id` matching no assistant call in the history) never reject the request — the history compiles as-is (tool name rendered `unknown`, ids verbatim) and the gateway logs a minimal warning. Tool-call ids persist across turns via an index derived per request from the request's own history (no server-side registry).
  - Unknown request fields (sampling knobs, `stream_options`, vendor extras) are accepted and ignored (lenient parsing, `extra="allow"`).
  - Unknown `model` → `404 model_not_found`; empty/missing `messages` or `model` → `422`.
- Authentication (M2): `Authorization: Bearer <DEEPSEEK_GATEWAY_API_KEY>` on `/v1/*`. Secure-by-default: unconfigured key → `503 GATEWAY_API_KEY_NOT_CONFIGURED` unless `GATEWAY_ALLOW_NO_AUTH=1` (ADR-017).
- Error envelope (M2): `{"error": {"message", "type", "code"}}`; `BackendFailure` categories map per the suggested HTTP table with `code` = category value (`app/error_mapping.py`).
- Reliability behavior (M9, ADR-036): TRANSIENT failures are absorbed by a bounded transport retry BEFORE the client sees them — at most `GATEWAY_MAX_RETRIES` retries (default 2 → at most 3 attempts) with deterministic linear backoff `GATEWAY_RETRY_BACKOFF_SECONDS × retry_number` (default 0.5 s → 0.5 s, 1.0 s; no jitter). Only taxonomy-retryable categories are retried (`RATE_LIMITED`, `UPSTREAM_NETWORK`, `UPSTREAM_5XX`, and truncation); `AUTH_INVALID`, `CLOUDFLARE_BLOCKED`, `UPSTREAM_PROTOCOL` (malformed data), `CLIENT_BAD_REQUEST`, `INTERNAL` make exactly ONE attempt — never a hot loop. Retry wraps only PRE-byte interactions (stream priming, buffered tool-turn drains, session creation, non-stream drains); once a response starts, failures surface as-is. The FINAL failure re-raises unchanged, so the public status/type/code after budget exhaustion is byte-identical to the no-retry mapping. Every upstream call is bounded by `DSQG_UPSTREAM_TIMEOUT_SECONDS` (default 60): an inactivity/stall timeout on the streaming call (silent sockets abort; healthy long streams survive) and a total timeout on control-plane calls. Request cancellation is intentionally not supported (ADR-036: the single-flight call gate cannot be released cross-thread).
- Conversation continuity (M4, ADR-020): resolved from the request's own message history — no conversation header exists or is required. A request whose history STRICTLY extends a stored canonical history continues that conversation: the gateway reuses the backend session, sends only the new trailing messages upstream, and threads `parent_message_id` (serialized to DeepSeek Web as the numeric u32 it requires, ADR-025 — live-verified). New, divergent, or duplicate (equal-history) requests start a fresh conversation compiled from the request's full history. Canonical history advances only when a turn completes; failures invalidate the backend link and the next request rebuilds from canonical state. Since M7 (ADR-028) this also applies after any MULTI-ATTEMPT tool turn (a bounded repair happened): the upstream session then contains an orphaned attempt branch, so the link is invalidated after committing the final result and the next request rebuilds from canonical history. State is in-memory only (bounded; lost on restart — continuity self-heals because requests carry their own history).
- Multi-account routing (M10, ADR-037): with `DSQG_ACCOUNT_TOKENS` configured, the gateway holds N accounts (`acct-1..N` in config order), each with its OWN backend instance. NEW conversations route to the USABLE account with the fewest active conversations (unused accounts first; ties → least recently used → config order); account states are `healthy` / `cooldown` / `invalid` (`disabled` is operator-derived, M12 surface). EXISTING conversations are sticky: a live backend session keeps its account — never round-robin per turn — even through a cooldown window; only an invalid/disabled account releases the link. Account consequences attach ONLY to FINAL failures (after the M9 retry budget): a 401-class failure marks the account `invalid` until operator action and releases its conversations' links (their next requests rebuild on a usable account through the standard ADR-020 rebuild path — full-history prompt); a 429-class failure puts the account into a bounded cooldown (`DSQG_ACCOUNT_COOLDOWN_SECONDS`, default 300) that blocks NEW conversations only; other categories only bump a failure counter. A dead-link rebuild prefers its bound account while that account remains sticky-usable (a final 429 never blocks the very conversation it failed). With NO usable account, a new conversation answers `429 RATE_LIMITED` when at least one account is still cooling down (client backoff) or `502 AUTH_INVALID` otherwise (operator action) — deterministic, secret-free messages. Single-account deployments (`DEEPSEEK_AUTH_TOKEN`) are byte-for-byte unchanged. Account state is in-memory (rebuilds from config on restart).
- Session failover (M11, ADR-038): with a multi-account fleet, a FINAL PRE-BYTE backend failure in an ACCOUNT-SCOPED category — 401-class `AUTH_INVALID` or 429-class `RATE_LIMITED`, the categories that invalidate the chosen account itself — triggers exactly ONE bounded in-request failover: the failing account takes its M10 consequence, the gateway selects another usable account (the failed one is excluded by its own consequence), establishes a new session there inside the normal M9 transport-retry wrapper, REHYDRATES the request's FULL canonical history through the same compilation path as a new conversation (assistant `tool_calls` and `role=tool` results round-trip as the same envelope / `[tool result]` blocks with ids intact), and re-runs the turn with `parent_message_id=None`. Failover NEVER chains: if establishment on the failover account fails, the ORIGINAL failure surfaces byte-identical (failover is best-effort transparency, not a new error contract); if the re-run fails, that failure surfaces as ITSELF with the failover account's consequence recorded. A committed failover turn rebinds the conversation to the failover account — the gateway never migrates it back. Failover covers every pre-byte site (session creation, stream priming, buffered tool-turn drains in both stream modes, non-stream drains); MID-stream failures (after any response byte) never fail over. Every ESTABLISHED failover session increments `session_failovers` (counted before the re-run) and logs exactly one INFO line `session failover: account <from> -> <to> (category=<c>)` — no tokens, no secrets. No new configuration; `/health` and `/admin/accounts` shapes unchanged. Single-account deployments are unaffected (failover requires a second usable account). Pinned by `tests/test_m11_failover.py`.
- Backend call serialization (ADR-027): the DeepSeek Web backend is single-flight PER ACCOUNT — since M10 (ADR-037) each account owns its own backend instance (own vendored client, PoW solver, call gate), so requests on DIFFERENT accounts run in parallel while concurrent requests routed to the SAME account queue at its backend boundary (the vendored client's shared wasmtime PoW solver and parser seam are not thread-safe; racing them crashed the process). Clients see normal responses, just serialized latency per account.
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

Since M10 (ADR-037) `ok` is fleet-aware: the default backend reports ready AND at least one enabled account is not `invalid`. The response shape is unchanged (single-account deployments keep the exact pre-M10 semantics until a final 401 retires the account).

Since M12 (ADR-039) this payload is produced by a single closure shared VERBATIM with `GET /admin/summary` (its `health` field) — the dashboard card and the probe can never diverge.

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

Implemented behavior (M6, ADR-023; buffered since M7, ADR-028; repair trigger widened by ADR-029/031/035): tool-enabled turns are drained through the envelope parser completely BEFORE any chunk is emitted; the outcome is then re-emitted through the same chunk renderer, so the public shapes below are byte-compatible with M6. Text before a valid envelope renders as normal `content` deltas; the sentinel itself is held back across chunk boundaries until the envelope is decided. A VALID envelope emits exactly two tool-call chunks — an opener (`index: 0`, id, `type: "function"`, name, empty arguments; plus `role` if not yet sent) and one arguments chunk with the full compact JSON — then the terminal chunk with `finish_reason: "tool_calls"` (overriding the backend reason). If no valid call emerged — whatever the reason (`required`, malformed/truncated attempt, simulation markers, pre-loop or marker-less mid-loop text; ADR-035) — ONE bounded repair retry runs before the honest fallback: the retry's outcome — valid call or plain text — is the ONLY thing rendered. Exactly one tool call per turn; repeated tool-result/model cycles are fully supported (each cycle is one buffered turn).

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

Implemented account-availability failure modes (M10, ADR-037): when NO account is usable for a NEW conversation, the gateway answers `429` with `code: "RATE_LIMITED"` while at least one account is still in its cooldown window (transient — the client's normal backoff applies), and `502` with `code: "AUTH_INVALID"` when the fleet is only invalid/disabled (operator action required). The messages are fixed and secret-free ("No usable backend account is available ..."). These are ROUTER-level failures — no backend is ever contacted.

## Optional future endpoints

Not part of core acceptance:

```text
/admin/accounts
/admin/health
/admin/sessions
```

(`/admin/metrics` was on this list and is IMPLEMENTED since M9, ADR-036; `/admin/accounts` is IMPLEMENTED read-only since M10, ADR-037 — lifecycle mutations added in M12, ADR-039; `/admin/sessions` is IMPLEMENTED since M12, ADR-039 as a metadata-only view — see Implementation status. `/admin/health` remains unimplemented — `GET /admin/summary` embeds the full `/health` payload instead.)

Do not let these delay core Qwen Code compatibility.
