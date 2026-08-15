# Upstream Notes

This document must be updated by the coding agent during M0 based on actual current behavior.

## Reference snapshot at starter creation

Checked 2026-08-13.

Repository:
https://github.com/xtekky/deepseek4free

Observed from the then-current source:

- Main client class: `DeepSeekAPI`
- Base URL in source: `https://chat.deepseek.com/api/v0`
- Constructor accepts an auth token
- Client contains PoW support
- Client loads cookie data from `dsk/cookies.json`
- It can create a chat session
- Chat completion accepts:
  - chat session ID
  - prompt string
  - optional parent message ID
  - thinking toggle
  - search toggle
- Completion is streamed
- Upstream parser reads SSE `data:` chunks and extracts content/type/finish reason
- Error types include authentication, rate limit, network, Cloudflare, and generic API errors
- README warns that DeepSeek API behavior may change frequently

## Current verification

Filled during M0 (2026-08-14) from source inspection plus offline verification.
Items marked **LIVE-PENDING** require a credential run of `scripts/probe_deepseek.py`.

### Commit/revision inspected

- Repository: https://github.com/xtekky/deepseek4free (MIT)
- HEAD commit: `4ae47bbb144f33b0ba855af9d1b0206ea794e16c` ("Merge pull request #9 — Added CloudFlare Bypass"), dated **2025-02-09**.
- Repo `pushed_at`: 2025-02-09. **Upstream has been dormant ~18 months.** There have been no upstream fixes for any DeepSeek-side changes since then; the private API must be assumed to have drifted. Live probe results decide whether the snapshot still works.
- Vendored under `vendor/deepseek4free/` (see `VENDOR_INFO.md` for checksums and the single `[DSQG-VENDOR-PATCH]`).

### Python/runtime requirements

- Upstream code is plain Python (3.9+ syntax; `tuple[int, int]` annotations). Verified to import and run on **Python 3.14.6 / Windows** after one patch: `import pkg_resources` replaced with `importlib.metadata` (pkg_resources no longer ships with setuptools >= 81 or stock 3.12+ venvs).
- PoW solver (`dsk/pow.py`) verified offline with `wasmtime==47.0.1` + `numpy==2.5.2`: WASM module `dsk/wasm/sha3_wasm_bg.7b9ca65ddd.wasm` instantiates and the exported functions (`__wbindgen_export_0`, `__wbindgen_add_to_stack_pointer`, `wasm_solve`) resolve with the current wasmtime-py API.
- Browser-automation deps (`nodriver`, `drissionpage`) are **not installed** by default; they are only needed for the interactive Cloudflare cookie refresh path (see below).

### Transport dependency constraints

- Upstream pins `curl-cffi==0.8.1b9`. **Not installable** on Python 3.12+/3.14 on Windows: no wheels; sdist build downloads `libcurl-impersonate v0.8.2` from a GitHub release that now 404s.
- Verified replacement: `curl-cffi==0.16.0` (cp310-abi3 wheel). All APIs used by the vendored client exist: `requests.request/post(..., impersonate='chrome120', stream=True, timeout=None)`, `response.iter_lines()`, `response.json()`, `requests.exceptions.RequestException`. `chrome120` is still an accepted impersonation target in 0.16.0.
- **M9 (ADR-036):** the vendored `timeout=None` (unlimited) is replaced by the annotated vendor patch `[DSQG-VENDOR-PATCH] M9` (`dsk/api.py DEFAULT_REQUEST_TIMEOUT`, set from `DSQG_UPSTREAM_TIMEOUT_SECONDS`, default 60 s). curl_cffi semantics: with `stream=True` the value becomes an INACTIVITY bound (connect timeout + `LOW_SPEED_LIMIT=1`/`LOW_SPEED_TIME`), so a silent socket aborts while a healthy long stream survives; on non-streaming calls it is a total timeout.
- Behavioral risk (RESOLVED 2026-08-14): TLS/HTTP2 fingerprint differences between curl-cffi 0.8.1b9 and 0.16.0 impersonation profiles were a concern; the live probe succeeded with 0.16.0 `chrome120` impersonation — no bot-detection rejection observed in three runs.

### Auth behavior

- Bearer token in `authorization` header against `https://chat.deepseek.com/api/v0`. Token obtained from browser localStorage `userToken` (README method).
- HTTP 401 → `AuthenticationError`; 429 → `RateLimitError`; >=500 → `APIError(status)`; other non-200 → `APIError(status)`. Mapped to the gateway taxonomy in `app/backends/deepseek_web/normalize.py::classify_upstream_exception` (unit-tested offline).
- Whether a current DeepSeek Web token still authenticates against `/api/v0`: **VERIFIED LIVE 2026-08-14** — a current 64-char token from `userToken.value` authenticated successfully; session creation and completion both worked with no cookies at all.

### Cookie/Cloudflare behavior

- Cookies load from `dsk/cookies.json` (`{"cookies": {...}}`), package-relative; missing file is a warning, not an error. The gateway instead injects cookies from a user-supplied file via `DeepSeekWebBackend(..., cookies_file=...)` — nothing is written into the vendor tree.
- `_make_request` detects Cloudflare interstitials ("Just a moment" HTML) on non-stream calls and then runs `dsk/bypass.py` **as a browser-launching subprocess** (nodriver/DrissionPage) to refresh `cf_clearance`, with max 2 retries. Because browser deps are not installed in the gateway venv, that path will fail loudly if triggered; the expected workaround is supplying a manually captured cookies file.
- The streaming completion path (`chat_completion`) has **no** Cloudflare retry at all — an interstitial there surfaces as a protocol/API error.
- Whether Cloudflare challenges appear for a given account/IP: **VERIFIED LIVE 2026-08-14** — no Cloudflare challenge appeared for the probe account/IP without any cookies. CF behavior may still vary by IP/account; the cookies-file path exists for that case.

### Session creation request/response

- `POST /chat_session/create` with body `{"character_id": null}`; session id read from `response['data']['biz_data']['id']`. `KeyError` → `APIError("Invalid session creation response format...")`.
- Real response shape: **VERIFIED LIVE 2026-08-14** — `data.biz_data.id` still present; session creation latency 0.55–1.2 s over three runs.

### Completion request shape

- PoW first: `POST /chat/create_pow_challenge` with `{"target_path": "/api/v0/chat/completion"}` → challenge object at `response['data']['biz_data']['challenge']` with fields `algorithm, challenge, salt, difficulty, expire_at, signature, target_path`. Solved via WASM sha3; answer sent base64-encoded JSON in header `x-ds-pow-response`.
- `POST /chat/completion` (streaming) with body:
  ```json
  {
    "chat_session_id": "...",
    "parent_message_id": null,
    "prompt": "...",
    "ref_file_ids": [],
    "thinking_enabled": true,
    "search_enabled": false
  }
  ```
- Notable headers: `x-app-version: 20241129.1`, `x-client-platform: web`, `x-client-version: 1.0.0-always`, Chrome-132-style user agent, `impersonate='chrome120'`.
- No native OpenAI-style `tools`/`messages` parameters: **the endpoint takes a single prompt string** — tool emulation (M6+) must compile the whole OpenAI message history into one prompt. Confirms the starter assumption.
- **VERIFIED LIVE 2026-08-14:** PoW challenge fetch + solve + streaming completion all succeeded. End-to-end turn latency ~1.6–2.3 s including PoW for one-word answers.

### Streaming event examples

**MAJOR LIVE FINDING (2026-08-14): the wire protocol changed completely.**
DeepSeek Web no longer streams OpenAI-style `choices[].delta` chunks — the
format the vendored parser expects. The current protocol is an event +
JSON-patch stream with sticky-path compression (full sanitized captures in
`tests/fixtures/deepseek_web/live/`):

```text
event: ready
data: {"request_message_id": "...", "response_message_id": "...", "model_type": "default"}

event: update_session
data: {"updated_at": 1786711092.380996}

data: {"v": {"response": {"message_id": "...", "parent_id": "...", "role": "ASSISTANT",
      "thinking_enabled": false, "status": "WIP", "content": "", "thinking_content": null,
      "search_status": null, "search_results": null, "tips": [], ...}}}

data: {"p": "response/content", "o": "APPEND", "v": "OK"}
data: {"p": "response/accumulated_token_usage", "o": "SET", "v": 39}
data: {"p": "response/status", "v": "FINISHED"}

event: finish
data: {}
event: title
data: {"content": "OK"}
event: close
data: {"click_behavior": "none", "auto_resume": false}
```

Protocol rules observed:

- Patch ops are `{"p": path, "o": op, "v": value}`. After a path is set,
  subsequent ops may omit `p` (**sticky path**) and even `o` (implicit
  APPEND on content paths). Verified with the thinking stream:
  ```text
  data: {"p": "response/thinking_content", "v": "1"}
  data: {"o": "APPEND", "v": "."}
  data: {"v": " The"}
  ```
- Text deltas: APPEND ops on `response/content` (short answers may arrive as
  a single APPEND).
- Thinking deltas: APPEND ops on `response/thinking_content` (token-level),
  followed by `response/thinking_elapsed_secs` SET bookkeeping.
- Terminal: `response/status` → `FINISHED` (only observed terminal value).
  The gateway maps it to `finish_reason='stop'` (ADR-013).
- Message ids: the `ready` event carries `request_message_id` +
  `response_message_id`; the snapshot's `parent_id` equals the request id.
- The vendored parser matched **zero** lines of real traffic. The gateway
  replaces it at runtime with a stateful adapter
  (`app/backends/deepseek_web/wire.py::WireSession`), keeping the vendored
  transport (HTTP, PoW, cookies, iteration, terminal break) intact.
- The legacy-format synthetic fixtures in
  `tests/fixtures/deepseek_web/synthetic/` remain as regression tests for
  the generic SSE/payload parser only; live captures are ground truth.

### Parent message behavior

- `parent_message_id` is accepted by the API call and sent verbatim (default `null`).
- The upstream README shows threaded follow-ups keyed on a `message_id` chunk field that the vendored parser **never emits** — the README example cannot work as written.
- **VERIFIED LIVE 2026-08-14:** threading identifiers ARE exposed in the current protocol — the `ready` event carries `request_message_id`/`response_message_id`, and the snapshot carries `message_id`/`parent_id` (parent_id == request id). The gateway adapter surfaces them as `BackendMessageId` events. Expected threading convention for M4: next turn's `parent_message_id` = previous turn's `response_message_id`. Actual multi-turn threading acceptance is an M4 test.
- Canonical local history (M4) still must not _depend_ on upstream memory; replaying compiled prompts into a fresh session remains the fallback.

### Thinking event behavior

- **VERIFIED LIVE 2026-08-14:** with `thinking_enabled=true`, thinking tokens stream as APPEND ops on `response/thinking_content` (34 deltas for a two-line reasoning trace), then `response/thinking_elapsed_secs` SET (e.g. `0.5958`), then content APPEND(s), then `FINISHED`. Thinking text and answer text are cleanly separated by path; the probe normalized this to `ReasoningDelta` ×34 + `TextDelta` ×1 + terminal `MessageFinished('stop')`.

### Error behavior

Verified offline (unit-tested classification). Live probing hit no error paths
(valid credential, no CF challenge, no rate limit across four runs), so live
error triggering remains untested:

| Upstream                                 | Gateway category              | Retryable           |
| ---------------------------------------- | ----------------------------- | ------------------- |
| `AuthenticationError` (401)              | AUTH_INVALID                  | no                  |
| `RateLimitError` (429)                   | RATE_LIMITED                  | yes (bounded, M9)   |
| `CloudflareError` / CF-giveup `APIError` | CLOUDFLARE_BLOCKED            | no                  |
| `NetworkError`                           | UPSTREAM_NETWORK              | yes                 |
| `APIError` status >= 500                 | UPSTREAM_5XX                  | yes                 |
| `APIError` other status                  | UPSTREAM_PROTOCOL             | no                  |
| `APIError` no status (JSON/parse)        | UPSTREAM_PROTOCOL             | no                  |
| malformed SSE JSON                       | UPSTREAM_PROTOCOL             | no                  |
| turn ends without a terminal marker (M9) | UPSTREAM_PROTOCOL (truncated) | yes (pre-byte only) |

Since M9 (ADR-036) the "Retryable" column is ENFORCED by the bounded
transport retry: retryable failures are retried up to the budget
(`GATEWAY_MAX_RETRIES`, default 2; linear backoff, no jitter) before the
client sees anything, non-retryable failures make exactly one attempt,
and the final failure keeps the exact no-retry HTTP mapping. Truncation
is the one deliberate override: the taxonomy default for
UPSTREAM_PROTOCOL is non-retryable, but a marker-less turn (a transient
upstream cut) is retried pre-byte; mid-stream truncation is never
retried (HTTP 200 already committed). All of this is pinned offline by
tests/test_m9_reliability.py; live triggering still unobserved.

### Deviations from starter assumptions

1. **THE WIRE PROTOCOL CHANGED** (most important): DeepSeek Web no longer streams OpenAI-style `choices[].delta` chunks. Current protocol is event + JSON-patch with sticky paths (see "Streaming event examples"). The vendored parser matched zero lines of real traffic; the gateway adapts the new protocol behind the backend boundary (ADR-013). This is exactly the drift risk the starter warned about.
2. **Upstream is dormant** (last push 2025-02-09) — the starter assumed an actively maintained integration; drift risk was higher than assumed (and materialized, per item 1).
3. **Dependency pin is dead** (`curl-cffi==0.8.1b9` uninstallable on modern Python; its prebuilt lib release 404s). Relaxed pin adopted (ADR-009).
4. **`pkg_resources` import broken** on current setuptools/Python — one minimal vendor patch applied (ADR-009).
5. **Cookies path is package-relative** (`dsk/cookies.json` inside the vendor dir); gateway injects cookies by attribute instead so no secret file lives in the vendor tree.
6. **Cloudflare auto-refresh launches a browser subprocess** (`dsk/bypass.py`) — not viable headless/by default; gateway treats cookies as user-supplied and the CF path as an explicit later feature (browser deps optional extra `cloudbypass`).
7. **README threading example is inconsistent** with the vendored parser (relies on a `message_id` field the parser never yields). Live verification showed threading ids ARE exposed via the `ready` event/snapshot; the gateway captures them (see Parent message behavior).
8. Starter assumption "client distinguishes authentication/rate-limit/network/API errors" — confirmed, plus a defined-but-unused `CloudflareError` (Cloudflare conditions surface via `APIError` message text instead).
9. **Transport/auth/PoW/session layers still work unchanged** — despite the protocol change, the vendored request path (headers, PoW WASM solver, endpoints, impersonation) succeeded live. Only the stream parser needed replacement.

## Rule

Never place a real token, cookie, account identifier, or sensitive response into this document.

## Qwen Code current verification

Docs verified 2026-08-14 against the live documentation site.
**Source-level wire verification completed 2026-08-14** against the public
repository (static source inspection). **Real-installation traffic captured
2026-08-14** during the M5 live acceptance run — see "Live traffic
verification" at the end of this section.

### Version tested

Docs current as of 2026-08-07/2026-08-12 (auth / model-providers pages).
Source inspected at `github.com/QwenLM/qwen-code`, branch `main`, commit
**`a669957f`** (2026-08-14); repo version **0.21.11** (`package.json`;
CHANGELOG 2026-08-13).

### Provider implementation files (source-level)

- Routing: `packages/core/src/core/contentGenerator.ts` — `AuthType.USE_OPENAI`
  dynamically imports `./openaiContentGenerator/index.js`.
- Implementation: `packages/core/src/core/openaiContentGenerator/` —
  `openaiContentGenerator.ts` (facade), `pipeline.ts` (request construction +
  stream consumption), `converter.ts` (messages/tools serialization, chunk
  conversion), `streamingToolCallParser.ts`, `errorHandler.ts`,
  `constants.ts`, `taggedThinkingParser.ts`, plus `provider/` sub-module
  (`default.ts` base + per-vendor providers incl. `deepseek.ts`, selected by
  `determineProvider()` on hostname/model).
- User `modelProviders` config processing: `packages/core/src/providers/provider-config.ts`.

### Provider settings format

Confirmed current (model-providers page, updated 2026-08-12):

```json
{
  "modelProviders": {
    "openai": [
      {
        "id": "deepseek-web",
        "name": "DeepSeek Web Gateway",
        "baseUrl": "http://127.0.0.1:8000/v1",
        "envKey": "DEEPSEEK_GATEWAY_API_KEY",
        "generationConfig": { "timeout": 120000, "maxRetries": 1 }
      }
    ]
  }
}
```

New since starter: custom provider ids possible via top-level `"providerProtocol": {"<id>": "openai"}`; the old wrapped `{protocol, models}` preview shape was reverted (bare arrays are canonical); wrapped entries in `$version: 4` settings are silently skipped.

### baseUrl behavior

Confirmed: `baseUrl` is passed to the official `openai` Node SDK, which appends resource paths — `http://127.0.0.1:8000/v1` (not `.../v1/chat/completions`). Models are unique by `id + baseUrl` within a provider.

Source-level (2026-08-14): `provider/default.ts → buildClient()` passes
`baseUrl` verbatim as `new OpenAI({ baseURL })`. The SDK is **pinned exactly**
at `"openai": "5.11.0"` (`packages/core/package.json`, no `^`). All default
base URLs end in `/v1`, and resource paths (`/chat/completions`,
`/embeddings`) are appended by the SDK (SDK-internal behavior; byte-level
UNVERIFIED outside the default-URL evidence).

### Plain request body

**Source-verified (pipeline.ts → buildRequest(), commit a669957f):**

```jsonc
{
  "model": "<configured model id>",
  "messages": [
    /* serialized by converter.ts, see below */
  ],
  // sampling fields ONLY when defined (priority: samplingParams config >
  // request > provider default): temperature, top_p, max_tokens, top_k,
  // repetition_penalty, presence_penalty, frequency_penalty
  "stream": true, // explicit; false on non-stream path
  "stream_options": { "include_usage": true }, // streaming only
}
```

- `stream` is ALWAYS explicit: agent turns stream (`true` + `stream_options`);
  the separate non-stream path (`pipeline.execute()`, used by side queries
  such as session titles) sends explicit `false` — code comment: some gateways
  wrongly default to SSE when the field is absent.
- `max_tokens` is ALWAYS injected by `default.ts → applyOutputTokenLimit()`
  unless `samplingParams` is set: env `QWEN_CODE_MAX_OUTPUT_TOKENS` or a 64K
  output ceiling; user-supplied values are honored verbatim even on unknown
  models (can be very large). `max_completion_tokens` is never produced by
  default (samplingParams pass-through only).
- `extra_body` is merged into the TOP-LEVEL request body, last (wins).
- `tools` only when non-empty; `tool_choice` only `'required'`/`'none'`
  (see below). Headers: `User-Agent: QwenCode/<version> (<platform>; <arch>)`
  plus user `customHeaders`.
- `embedContent()` calls `{baseURL}/embeddings` with model HARDCODED to
  `text-embedding-ada-002`.
- Documented config knobs (docs level): provider `generationConfig` is
  **impermeable** (applied atomically; top-level `model.generationConfig` is
  NOT inherited for provider models); `samplingParams`/`customHeaders`/
  `extra_body` are atomic replacements; `extra_body` is only honored for
  OpenAI-compatible providers.

### tools[] body

**Source-verified (converter.ts → convertGeminiToolsToOpenAI):**

```jsonc
{
  "type": "function",
  "function": {
    "name": "<tool>",
    "description": "<or ''>",
    "parameters": {
      /* JSON Schema */
    },
  },
}
```

- `parameters` from MCP `parametersJsonSchema` directly, or converted Gemini
  schemas; then `convertSchema(...)` + `relaxSchemaForFunctionCalling()`
  (issue #7315: gateways enforcing OpenAI's structured-output contract
  promote every property to required when `additionalProperties: false`, so
  Qwen Code relaxes the wire schema).
- **No `strict` flag is ever set.** `parallel_tool_calls` is never sent
  (verified in every inspected request builder; GitHub repo-wide code search
  unavailable without auth — UNVERIFIED outside those files). Tool
  parallelism is executed client-side.

### tool_choice behavior

**Source-verified:** only `'required'` (mode ANY) or `'none'` (mode NONE) —
`'auto'` is never sent. (DashScope provider drops `'required'` while thinking
is active; irrelevant to this gateway.)

### assistant tool_calls history

**Source-verified (converter.ts):** assistant messages serialize as:
`content` = concatenated text parts; **`null`** when the message is
tool-calls-only; **`''`** when reasoning is present without text (Ollama
compatibility). Tool calls:

```jsonc
"tool_calls": [{ "id": "<callId or 'call_<i>'>", "type": "function",
  "function": { "name": "<normalizeMcpToolName(...)>",
                "arguments": "<JSON.stringify(args)>" } }]
```

Thought parts serialize to `reasoning_content` (also mirrored to a
`reasoning` field for qwen3 models via default.ts). Post-processing
`mergeConsecutiveAssistantMessages` + `cleanOrphanedToolCalls` run by
default.

### role=tool result shape

**Source-verified:** `{ "role": "tool", "tool_call_id": "<response.id or ''>",
"content": [...] }` — content defaults to an ARRAY of text parts; a plain
string when `toolResultContentFormat: 'string'`; empty result → `content: ''`.
`splitToolMedia` (default true) moves media out of tool messages into a
follow-up user message ("OpenAI spec only permits string/text-part content on
tool messages").

### Streaming tool-call expectations

**Source-verified (pipeline.ts, streamingToolCallParser.ts):**

- SSE parsing is done by the openai SDK; there is NO explicit `[DONE]`
  handling in application code (SDK behavior; UNVERIFIED at SDK level).
- `delta.content` → text part; `delta.reasoning_content` (or
  `delta.reasoning`) → thought part; if empty, tagged `<think>` parsing
  (TaggedThinkingParser) is used as leak fallback.
- `delta.tool_calls` → `StreamingToolCallParser.addChunk(index, argsFragment,
id, name)`, keyed by `index`: the opener chunk carries `id`+`name`
  (arguments may be `""`); subsequent fragments on the same index continue
  with or without repeated ids. Arguments are parsed with `JSON.parse` plus
  brace/bracket/string depth tracking and orphan-closing-brace repair;
  duplicate ids across indices are detected and remapped. Missing ids fall
  back to `call_<index>`.
- Stream end: SDK iterator completion (after `[DONE]`). The pipeline defers
  yielding the finish chunk to merge it with a trailing usage chunk
  (requested via `stream_options: {include_usage: true}`) and flushes a
  pending finish chunk at stream end if usage never arrives — **a missing
  usage chunk is tolerated**. Duplicate finish chunks are tolerated
  (OpenRouter-style providers).
- `finish_reason === 'error_finish'` → `StreamContentError`. A streaming
  response with non-SSE content-type (e.g. an HTML 200 page) →
  `NonSSEResponseError`.

### Finish-reason expectations

**Source-verified (converter.ts → mapFinishReason):** `stop`→STOP;
`length`→MAX_TOKENS; `content_filter`→SAFETY; `function_call`/`tool_calls`→
STOP; null/undefined/unknown→UNSPECIFIED (tolerated). Tool execution is
decided from functionCall parts, NOT from finish_reason. Override: when
`usage.completion_tokens` passes a threshold while a tool call is incomplete,
the converter forces MAX_TOKENS ("tool call JSON is likely truncated" →
repair flow).

### Error/retry behavior

**Source-verified (constants.ts, pipeline.ts, geminiChat.ts, utils/retry.ts):**

- SDK level: `maxRetries = 3`, `timeout = 120000 ms` defaults passed to
  `new OpenAI({...})` (`timeout: 0` disables). The SDK's own 408/409/429/5xx
  retries are SDK behavior (UNVERIFIED in-repo).
- Stream guards: idle-without-chunk 240 s (`QWEN_STREAM_IDLE_TIMEOUT_MS`) and
  lifetime cap 900 s (`QWEN_STREAM_MAX_LIFETIME_MS`) → retryable `ETIMEDOUT`.
- App level (`retryWithBackoff` predicate): 429 retried; 5xx retried;
  **400 NEVER retried** (single exception: a 400 demanding
  `enable_thinking ... must be true` rebuilds the request once without the
  thinking-disable field); exponential backoff 1.5 s → 30 s (×2), honors
  `Retry-After` on 429/503; unattended/persistent mode retries 429/529
  indefinitely with a 5-minute backoff cap.
- Mid-stream transport retries for `ECONNRESET`/`ETIMEDOUT`/`UND_ERR_*`:
  up to 2 full replays when nothing was delivered yet, else up to 3
  "continuation" retries (model asked to continue truncated output).
- Retry amplification: SDK(3) × app backoff × transport replay — a 429/5xx
  can produce many repeat requests; the gateway's own retries (M9) must stay
  bounded to avoid compounding.
- Errors must arrive as HTTP status codes for correct classification; an
  error delivered as a 200 SSE chunk is only recognized via
  `finish_reason: 'error_finish'`.

### Extra fields/extensions

Documented: `reasoning` handling on OpenAI-compatible paths (honored unless `samplingParams` set; `samplingParams.reasoning_effort`/`extra_body` sent verbatim; DeepSeek-specific rewrites apply only to `api.deepseek.com`, not self-hosted endpoints). `customHeaders` supported per provider entry.

Source-verified (2026-08-14): depending on config/model/provider, NON-STANDARD
fields may appear in request bodies: `reasoning` (top-level `{effort,
budget_tokens}`), `reasoning_effort`, `enable_thinking`, `thinking_budget`,
`thinking: {"type": "disabled"}` (emitted when reasoning is disabled, gated
to DeepSeek hostnames — V4+ comment), `chat_template_kwargs:
{enable_thinking: false}` (qwen-family models on non-DashScope providers),
`preserve_thinking`, `metadata`, `cache_control`, `vl_high_resolution_images`.
**The gateway must tolerate and ignore unknown request-body fields** (no
strict body validation). The built-in DeepSeek provider (`provider/deepseek.
ts`, active only for hostname `api.deepseek.com` or deepseek-named models)
flattens content arrays to plain strings, defaults `temperature: 0`, and maps
reasoning effort → top-level `reasoning_effort`; none of those hostname-gated
paths apply to this gateway (127.0.0.1), which is served by the default
provider path.

### Qwen Code deviations from starter assumptions

1. Starter's `security.auth.selectedType: "openai"` still valid (auth page, updated 2026-08-07); however `security.auth.apiKey` / `security.auth.baseUrl` are **deprecated** (removed flow since v0.10.1) — keys come from `envKey` env vars / `.env` / settings `env`.
2. `generationConfig` semantics are stricter than the starter sample implied (impermeable, atomic fields) — the gateway-facing Qwen Code config doc in `QWEN_CODE_INTEGRATION.md` must keep the whole generation config inside the provider entry.
3. Qwen OAuth free tier discontinued 2026-04-15 — irrelevant to the gateway but confirms API-key/OpenAI-compatible is the supported path.
4. QWEN.md memory behavior confirmed: root `QWEN.md`, `.qwen/QWEN.local.md`, `~/.qwen/QWEN.md`, and `AGENTS.md` (if present) are all loaded; `/memory` lists them. Starter layout is compatible.
5. `stream` is always sent explicitly (`true` + `stream_options:{include_usage:true}` on agent turns; explicit `false` on side queries) — the gateway must not default to SSE when the field is absent (their code comment explicitly warns about gateways that do).
6. `tool_choice` is only ever `'required'` or `'none'` — never `'auto'`; no `parallel_tool_calls`; no `strict` tool schemas. The tool emulator (M6+) must not rely on any of these.
7. `max_tokens` is always present and may exceed backend limits (user values honored verbatim) — the gateway should clamp silently and document it, rather than reject.
8. The client is highly tolerant of wire quirks (missing/null `finish_reason`, duplicate finish chunks, `content: null` on tool-only assistant messages, missing tool*call ids with `call*<index>` fallback) — emitting standard OpenAI shapes is safe.
9. `embedContent()` hardcodes model `text-embedding-ada-002` against `{baseURL}/embeddings` — if embeddings are ever exercised, the gateway must alias that model or fail clearly (out of core milestones).
10. openai Node SDK is pinned exactly at `5.11.0` — wire expectations are stable per that SDK version.

### Live traffic verification (M5 acceptance, 2026-08-14)

A real Qwen Code **v0.21.11 (win32; x64)** installation was connected to the
gateway with the diagnostic capture layer enabled; 9 requests were recorded
during a successful plain-chat session. Structural diff against the
source-verified expectations above:

**Confirmed as source-verified:**

- User-Agent `QwenCode/0.21.11 (win32; x64)`; `Authorization: Bearer`
  present on every request.
- Agent turns: explicit `stream: true` + `stream_options:
{"include_usage": true}`; side queries send explicit `stream: false`
  with NO `stream_options` key.
- `tools[]` shape uniform `{type: "function", function: {name, description,
parameters}}`; no `strict`, no `parallel_tool_calls` — observed at scale
  (up to 69 tools in one request, including MCP-provided tools).
- `tool_choice` observed only as `'required'` (structured side queries);
  absent on agent turns. Never `'auto'`.
- `max_tokens` always present (value 32000 on this install — a user
  setting, honored verbatim per `applyOutputTokenLimit`).
- Multi-turn assistant history carried plain string content (the gateway
  never emitted tool_calls, so the tool-history 400 stayed unreachable, as
  designed in M5).
- Non-standard extras: NONE observed in this session (the leniency fixtures
  remain defensive).
- All 9 requests answered 200; Qwen Code functioned normally end-to-end.

**Corrections folded back into fixtures:**

1. `max_tokens` was 32000, not the 32768 used in the synthesized fixture.
2. `temperature: 0` present on every request (fixtures had omitted it).
3. New request class: a **structured side query** fires alongside each user
   submission — `stream: false` + `tool_choice: 'required'` + a single
   `respond_in_schema` tool with a small system prompt. The gateway answers
   plain text (tools ignored, ADR-021) and Qwen Code tolerates that.
   Fixtured as `side_query_respond_in_schema.json`.

**Observations not yet fully explained (monitor):**

- A parallel request lineage ran with a different (smaller) system prompt
  and a reduced 7-tool set (read_file, grep_search, glob, list_directory,
  run_shell_command, write_file, edit), inheriting the parent conversation
  messages — ORIGIN-UNCONFIRMED, probably a Qwen Code delegation/subagent
  pipeline. Served fine (200 plain text).
- Both user turns were re-submitted byte-identically 1–5 s after the first
  attempt before succeeding. Capture is request-side only, so the first
  attempt's outcome is unknown; consistent with an SDK/app retry after a
  5xx/stream interruption or the client's SSE auto-resume. If this
  reproduces with visible errors, check gateway logs for the first attempt.

Privacy: raw captures remain in the user's private diagnostics directory
(they contain real prompts); only structural facts are recorded here.

### Live multi-turn tool-loop verification (M7 probe, 2026-08-15)

A live probe through the full gateway pipeline (stream=true, two declared
tools, local tool execution fed back as `role=tool` history) confirmed the
upstream behaviors M7 depends on:

- DeepSeek followed the control-envelope protocol on the FIRST TRY for
  every turn of a 4-turn sequence: `list_directory` → `read_file` →
  `read_file` → final answer. All three tool turns arrived as clean
  envelopes (no malformed/truncated attempts), so the bounded repair path
  (ADR-028) was never needed live.
- Delta turns carrying compiled `[tool result]` blocks continue the same
  backend session correctly — turn 4's answer demonstrably used content
  from the files read on turns 2–3 (it quoted both). Wall time 13.5 s for
  the whole loop; every turn landed on the session-reuse/delta path.
- The model answered HONESTLY from truncated tool results ("not fully
  shown in the excerpt") rather than hallucinating missing content — tool
  results as DATA in the compiled prompt behave as designed (ADR-023).
- Repair remains unverifiable on demand live (a cooperating model cannot
  be forced to malform an envelope); its evidence is the offline suite
  (tests/test_m7_loop.py). If future live runs show two backend calls per
  `tool_choice: "required"` turn, the repair path fired (by design).

### Prose-simulated tool use and the ADR-029 replay (2026-08-15)

The user's first M7 acceptance attempt surfaced a NEW upstream behavior
(not a gateway wire bug): on the very first agent turn (69 tools,
`tool_choice` absent) the model answered in PROSE — narrating a simulated
tool loop ("Saya akan menampilkan daftar file…") with a fabricated `docs/`
listing (reassembled from the QWEN.md reading list inside the system
prompt) and fake "read" summaries. `finish_reason: stop`, zero tool calls,
no envelope attempted — so neither the M7 repair trigger nor any parser
flag fired. A model without native function calling falls back to
NARRATING actions when the prompt does not explicitly forbid simulating
them. The same stochastic wrinkle appeared once during M6 acceptance
(hallucinated shell block).

ADR-029 (docs/DECISIONS.md) counters it deterministically: the tool
instructions now forbid simulated/narrated tool execution, and the bounded
repair also fires on PRE-loop envelope-less plain text (history carries no
assistant tool call yet). Live re-verification replayed the EXACT captured
request through the patched gateway against the real backend: the outcome
flipped from stop/2,349 chars of simulated prose to `finish_reason:
tool_calls` with a real `list_directory` call
(`{"path":"D:\\deepseek-agent-gateway-starter\\docs"}`), HTTP 200 in 2.6 s.
Upstream facts confirmed by this replay: (1) the model CAN follow the
envelope protocol on a 69-tool agent turn once the anti-simulation wording
is present (and/or the repair hint follows a simulated attempt); (2) the
threaded session accepted the re-branch on the original parent without
error. Capture remains request-only, so which attempt produced the call is
not recorded; the replay script was deleted after use.
