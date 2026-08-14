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

| Upstream                                 | Gateway category   | Retryable         |
| ---------------------------------------- | ------------------ | ----------------- |
| `AuthenticationError` (401)              | AUTH_INVALID       | no                |
| `RateLimitError` (429)                   | RATE_LIMITED       | yes (bounded, M9) |
| `CloudflareError` / CF-giveup `APIError` | CLOUDFLARE_BLOCKED | no                |
| `NetworkError`                           | UPSTREAM_NETWORK   | yes               |
| `APIError` status >= 500                 | UPSTREAM_5XX       | yes               |
| `APIError` other status                  | UPSTREAM_PROTOCOL  | no                |
| `APIError` no status (JSON/parse)        | UPSTREAM_PROTOCOL  | no                |
| malformed SSE JSON                       | UPSTREAM_PROTOCOL  | no                |

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

Docs verified 2026-08-14 against the live documentation site; source-level wire
verification in progress (M5 will complete it with a real installation).

### Version tested

Docs current as of 2026-08-07/2026-08-12 (auth / model-providers pages). Sidebar
references `qwen serve (v0.16-alpha)`. Installed-version checks happen in M5.

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

### Plain request body

Source-level verification pending (M5). Documented knobs that shape it: provider `generationConfig` is **impermeable** (applied atomically; top-level `model.generationConfig` is NOT inherited for provider models); `samplingParams`/`customHeaders`/`extra_body` are atomic replacements; `extra_body` is only honored for OpenAI-compatible providers.

### tools[] body

Source-level verification pending (M5).

### tool_choice behavior

Source-level verification pending (M5).

### assistant tool_calls history

Source-level verification pending (M5).

### role=tool result shape

Source-level verification pending (M5).

### Streaming tool-call expectations

Source-level verification pending (M5).

### Finish-reason expectations

Source-level verification pending (M5).

### Extra fields/extensions

Documented: `reasoning` handling on OpenAI-compatible paths (honored unless `samplingParams` set; `samplingParams.reasoning_effort`/`extra_body` sent verbatim; DeepSeek-specific rewrites apply only to `api.deepseek.com`, not self-hosted endpoints). `customHeaders` supported per provider entry.

### Qwen Code deviations from starter assumptions

1. Starter's `security.auth.selectedType: "openai"` still valid (auth page, updated 2026-08-07); however `security.auth.apiKey` / `security.auth.baseUrl` are **deprecated** (removed flow since v0.10.1) — keys come from `envKey` env vars / `.env` / settings `env`.
2. `generationConfig` semantics are stricter than the starter sample implied (impermeable, atomic fields) — the gateway-facing Qwen Code config doc in `QWEN_CODE_INTEGRATION.md` must keep the whole generation config inside the provider entry.
3. Qwen OAuth free tier discontinued 2026-04-15 — irrelevant to the gateway but confirms API-key/OpenAI-compatible is the supported path.
4. QWEN.md memory behavior confirmed: root `QWEN.md`, `.qwen/QWEN.local.md`, `~/.qwen/QWEN.md`, and `AGENTS.md` (if present) are all loaded; `/memory` lists them. Starter layout is compatible.
