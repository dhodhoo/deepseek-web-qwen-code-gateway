# Progress Log

The coding agent must update this file after every milestone.

## Current status

**Current milestone:** M3 — OpenAI SSE streaming

**State:** COMPLETE (offline-verified + live curl smoke 2026-08-14). Awaiting user review; M4 not started.

## Completed

- Starter architecture/specification created.
- M0 (2026-08-14): full upstream compatibility spike, including live probe.
- M1 (2026-08-14): stable backend interface, FakeBackend, configuration boundary, import-boundary guard.
- M2 (2026-08-14): FastAPI gateway surface — /health, /v1/models, non-streaming /v1/chat/completions with bearer auth, deterministic message compiler, OpenAI error mapping.
- M3 (2026-08-14): OpenAI SSE streaming — event→chunk translator, primed pre-stream error handling, in-stream mid-failure envelope, [DONE], disconnect-safe generator.

## Tests run

```text
.venv\Scripts\python.exe -m pytest -q
228 passed, 2 deselected (live tests excluded by default marker)

Live curl smoke (GATEWAY_BACKEND=fake): stream:true returned incremental
chat.completion.chunk lines + data: [DONE]; pre-stream failure answered a
real HTTP status with the OpenAI error body; non-stream chat unaffected.
```

## Known limitations

- Live error paths (429/5xx/Cloudflare) were not triggered during probing; classification is unit-tested offline only.
- Multi-turn threading (parent_message_id = previous response_message_id) is captured but not yet exercised end-to-end (M4).
- Upstream deepseek4free is dormant since 2025-02-09; its stream parser was fully obsolete (protocol changed). Further drift is possible at any time; probe captures are the early-warning mechanism.
- Each request creates a fresh backend session (no conversation reuse until M4); sampling parameters are accepted but ignored; no usage chunk in streams (no upstream token counts).
- Reasoning/thinking content is intentionally NOT surfaced in M3 streams.
- Tool calling, Qwen Code provider wiring, multi-account, UI, Docker intentionally not started.

## Next action

User reviews the M3 report. If approved, start M4 (canonical conversation/session state, multi-turn).

---

## 2026-08-14 — M3: OpenAI SSE streaming

### Completed

- Single translator `app/streaming.py` (ADR-019): normalized events → OpenAI `chat.completion.chunk` SSE lines. `MessageStarted` → role chunk (role force-injected if the backend skips it), `TextDelta` → incremental content, `MessageFinished` → terminal chunk with mapped finish reason, then `data: [DONE]`. `ReasoningDelta`/`BackendMessageId`/`UnknownDelta` render to nothing — vendor-internal data never crosses the wire (the M3 exit criterion, guarded by construction + tests).
- Pre-stream error handling via priming: the route pulls the first backend event before returning the `StreamingResponse`, so failures before the first byte still answer real HTTP statuses (client retry semantics preserved); mid-stream failures emit `data: {"error": ...}` and close WITHOUT `[DONE]`.
- BackendError event re-decision (ADR-011/014 follow-through): exceptions remain the canonical failure surface; BackendError events are defensively normalized into the same failure path on both sides of priming.
- Threading/disconnect: blocking backend iterator consumed via `iterate_in_threadpool`; client disconnect closes the async generator cleanly (aclose-tested); degenerate empty turns emit a well-formed role+finish+`[DONE]` sequence; `stream_options.include_usage` tolerated with no usage chunk (documented honesty).
- Server wiring (`app/server.py`): `stream:true` now returns `StreamingResponse(text/event-stream)`; validation order unchanged (model 404 / tools 400 / compile 400 apply to both modes).

### Files changed

```text
app/streaming.py (new)
app/server.py (stream path: _start_stream_response + priming; 501 removed)
tests/test_streaming.py (new), tests/test_api_streaming.py (new)
tests/test_api.py (stream:true expectation updated from 501 to SSE)
docs/DECISIONS.md (ADR-019), docs/API_CONTRACT.md (implementation status), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
228 passed, 2 deselected in 2.83s   (195 M0-M2 tests + 33 new M3 tests)

Live curl smoke against uvicorn (GATEWAY_BACKEND=fake, stream:true):
  data: {...delta:{"role":"assistant","content":""}...}
  data: {...delta:{"content":"Hello "}...}   (incremental, same id/created/model)
  data: {...delta:{"content":"from "}...}
  data: {...delta:{"content":"SSE!"}...}
  data: {...delta:{},"finish_reason":"stop"}
  data: [DONE]
  Second request (script exhausted) -> HTTP 500 + OpenAI error body (pre-stream failure as HTTP status)
```

### Upstream observations

None new — M3 runs against `FakeBackend` only; no DeepSeek traffic. Live streaming against chat.deepseek.com becomes observable once M4 session state (or a manual probe) drives multi-event turns.

### Known limitations

- Client disconnect stops emission, but the in-flight upstream turn still runs to completion in the threadpool (stateless sessions — nothing to roll back); upstream cancellation is M9 scope.
- No usage chunk (no upstream token counts; client tolerates absence — source-verified).
- Reasoning content is dropped from public streams in M3 (surfacing it is a later explicit decision).
- Streaming tool_calls are still rejected with the rest of tools (400, M6).

### Decisions added/changed

- ADR-019 streaming surface: single translator; primed HTTP-status errors before the first byte; in-stream error envelope without [DONE] mid-stream; no-leak rendering rules; no usage chunk; threadpool + disconnect semantics. Also closes the ADR-011/014 re-decision: exceptions remain canonical, BackendError events inventory-only.

### Next milestone

M4 — Canonical conversation/session state: normalized message history, backend session mapping, parent-message mapping, tool-history-capable representation, reconstruction tests; exit "multi-turn plain chat is correct and locally reconstructable" (awaiting explicit user approval).

---

## 2026-08-14 — M2: Basic OpenAI Chat Completions

### Completed

- OpenAI-compatible wire schemas (`app/openai_types.py`): request models lenient (`extra="allow"`, per Qwen source verification), response models strict standard shapes (ADR-018).
- Deterministic message compiler (`app/prompt_compiler.py`): `system`/`user`/`assistant` text → labeled-block prompt; content lists reduced to text parts; tool-shaped messages rejected with milestone pointers (M6).
- Error mapping (`app/error_mapping.py`): `BackendFailure` categories → OpenAI error envelope + contract HTTP statuses; `code` = stable category value.
- Configuration extended (`app/config.py`): `DEEPSEEK_GATEWAY_API_KEY` (SecretStr), `GATEWAY_ALLOW_NO_AUTH`, `GATEWAY_MODEL_ID`, `GATEWAY_HOST`, `GATEWAY_PORT`.
- FastAPI application (`app/server.py`) + entry point (`app/main.py`): `GET /health` (open), `GET /v1/models`, `POST /v1/chat/completions` (non-streaming only; 501 for `stream:true`, 400 for tools, 404 unknown model); secure-by-default bearer auth (ADR-017); sync handlers on Starlette's threadpool; fresh backend session per request.
- Dependencies added: `fastapi`, `uvicorn` (runtime), `httpx` (dev, for TestClient).

### Files changed

```text
app/openai_types.py (new), app/prompt_compiler.py (new), app/error_mapping.py (new)
app/server.py (new), app/main.py (new)
app/config.py (gateway key/auth/model/host/port settings)
tests/test_prompt_compiler.py (new), tests/test_error_mapping.py (new), tests/test_api.py (new)
pyproject.toml (fastapi, uvicorn; dev: httpx), .env.example (M2 variables)
docs/DECISIONS.md (ADR-017..018), docs/API_CONTRACT.md (implementation status), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
195 passed, 2 deselected in 2.73s   (129 M0/M1 tests + 66 new M2 tests)

Live curl smoke against uvicorn (GATEWAY_BACKEND=fake):
  POST /v1/chat/completions (Bearer key, plain chat)
    -> 200 {"object":"chat.completion","choices":[{"message":{"role":"assistant","content":"Hello from curl!"},"finish_reason":"stop"}],...}
  GET /health -> 200 {"ok":true,...}; GET /v1/models -> 200 alias list
  no auth -> 401 invalid_api_key; stream:true -> 501 STREAMING_NOT_YET_SUPPORTED
  unscripted fake backend -> 500 INTERNAL (OpenAI error envelope)
```

### Upstream observations

None new — M2 runs against `FakeBackend` only; no DeepSeek traffic. (Qwen Code client-side wire notes from the source verification remain the reference for M3/M5/M6.)

### Known limitations

- Non-streaming only: `stream:true` is rejected 501 (M3). Qwen Code agent turns always stream, so M3 is the next hard requirement before any client wiring.
- One fresh backend session per request; no conversation continuity (M4 canonical state).
- Sampling parameters (`temperature`, `max_tokens`, ...) are accepted and ignored — documented leniency, nothing to map onto upstream yet.
- `tools: []` (empty list) is treated as plain chat, not rejected.

### Decisions added/changed

- ADR-017 gateway API auth: secure-by-default bearer key, 503 when unconfigured, explicit dev opt-out, constant-time compare.
- ADR-018 M2 HTTP surface: non-stream only, explicit honest rejections (501/400 with milestone pointers), lenient request parsing, session-per-request.

### Next milestone

M3 — Streaming chat completions: OpenAI SSE chunks out (`chat.completion.chunk`), `data: [DONE]` termination, incremental text deltas from `LLMBackend.stream_turn`, streaming error handling (awaiting explicit user approval).

---

## 2026-08-14 — M1: Backend abstraction

### Completed

- Defined the stable backend contract `app/backends/base.py`: `LLMBackend` ABC with `backend_type`, `health_check() -> BackendHealth`, `create_session() -> BackendSession`, `stream_turn(...) -> Iterator[BackendEvent]` (ADR-014).
- Conformed `DeepSeekWebBackend` to the contract: typed returns, first positional renamed `chat_session_id` → `session_id`; `raw_sink` documented as a non-portable backend extension.
- Implemented `FakeBackend` (`app/backends/fake.py`): scripted deterministic turns, recorded calls, sequential fake sessions, strict script-exhausted failure; plus `fake_text_turn()` helper (ADR-014/015).
- Configuration boundary `app/config.py`: `GatewaySettings.from_env()` (injectable env mapping), `SecretStr` token masking, `ConfigError` messages without values, `build_backend()` registry with lazy backend imports; `GATEWAY_BACKEND` selects `deepseek_web` (default) or `fake` (ADR-015). Added `pydantic>=2` runtime dependency; `.env.example` documents `GATEWAY_BACKEND`.
- Import-boundary guard `tests/test_import_boundary.py`: AST scan of `app/` + `scripts/` fails on any `dsk` import outside `app/backends/deepseek_web/`; `tests/` exempt by documented design; detector self-tested (ADR-016).
- Adapted probe script, offline backend tests, and live tests to the typed interface.

### Files changed

```text
app/backends/base.py (new), app/backends/fake.py (new), app/config.py (new)
app/backends/__init__.py (public exports), app/backends/deepseek_web/backend.py (interface conformance)
scripts/probe_deepseek.py (BackendSession usage)
tests/test_backend_interface.py (new), tests/test_fake_backend.py (new),
tests/test_config.py (new), tests/test_import_boundary.py (new)
tests/test_backend_offline.py, tests/test_live_upstream.py (typed interface)
pyproject.toml (pydantic>=2), .env.example (GATEWAY_BACKEND)
docs/DECISIONS.md (ADR-014..016), docs/ARCHITECTURE.md (concrete interface), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
129 passed, 2 deselected in 1.29s   (95 M0 tests + 34 new M1 tests)

.venv\Scripts\python.exe scripts\probe_deepseek.py --help
OK (import/parse smoke test after interface change; no live run in M1)
```

### Upstream observations

None new — M1 is internal refactoring; no upstream traffic. `docs/UPSTREAM_NOTES.md` unchanged (Qwen source-level verification still pending from the paused research task; feeds M5).

### Known limitations

- `BackendError` events remain inventory-only; the canonical failure surface across the interface is `BackendFailure` exceptions (re-decision due at M3 async streaming, per ADR-011/014).
- `FakeBackend` validates nothing about session ids passed to `stream_turn` (deliberate simplicity).
- No HTTP surface exists yet — `build_backend`/`GatewaySettings` are exercised only by tests until M2.

### Decisions added/changed

- ADR-014 stable `LLMBackend` ABC + typed value returns
- ADR-015 configuration boundary (pydantic v2 + SecretStr + factory registry)
- ADR-016 import-boundary guard with documented tests exemption

### Next milestone

M2 — Basic OpenAI-compatible chat: FastAPI app, `POST /v1/chat/completions` (non-stream first), `GET /health`, `GET /v1/models`, gateway API key auth, OpenAI error mapping — all against the stable `LLMBackend` interface (developable fully offline via `GATEWAY_BACKEND=fake`).

---

## 2026-08-14 — M0: DeepSeek upstream compatibility spike

### Completed

- Read full starter spec; inspected current upstream `deepseek4free` (commit `4ae47bbb`, dormant since 2025-02-09) and current Qwen Code docs (model-providers/auth/memory, updated 2026-08-07/12).
- Initialized Python 3.14 project (pyproject.toml, pytest with `live` marker, .gitignore, .env.example); git repository initialized.
- Vendored deepseek4free at pinned commit (MIT preserved; provenance + checksums in `vendor/deepseek4free/VENDOR_INFO.md`); one minimal `[DSQG-VENDOR-PATCH]` (`pkg_resources` → `importlib.metadata`).
- Verified transport deps: `curl-cffi==0.8.1b9` uninstallable on modern Python → relaxed to 0.16.0 (chrome120 impersonation verified); wasmtime 47.0.1 PoW solver verified offline.
- Implemented normalized event model (`app/backends/events.py`), error taxonomy + mapping (`errors.py`, `normalize.py`), `DeepSeekWebBackend` spike (`backend.py`), fixture sanitization (`sanitize.py`), and the current-protocol wire adapter (`wire.py::WireSession`).
- Added `scripts/probe_deepseek.py` (credential-safe, writes sanitized fixtures, exit codes per error category).
- **Live probe with user-provided credential:** client init, session creation, one prompt, streamed output, thinking stream, terminal finish — all verified. Sanitized fixtures captured.
- **Major finding:** DeepSeek Web's stream protocol changed to event + JSON-patch with sticky paths; vendored parser obsolete; adapter implemented and tested (ADR-013).

### Files changed

```text
pyproject.toml, .gitignore, .env.example, README/START files unchanged
app/__init__.py
app/backends/__init__.py, events.py, errors.py
app/backends/deepseek_web/__init__.py, _vendor.py, backend.py, normalize.py, sanitize.py, wire.py
scripts/probe_deepseek.py
tests/conftest.py, test_sse_parser.py, test_normalize.py, test_errors.py,
tests/test_sanitize.py, test_backend_offline.py, test_wire.py, test_live_upstream.py
tests/fixtures/deepseek_web/{README.md, synthetic/*.sse.txt, live/stream_*.sse.txt, live/meta_*.json}
vendor/deepseek4free/** (pinned snapshot + VENDOR_INFO.md + one marked patch)
docs/DECISIONS.md (ADR-009..013), docs/UPSTREAM_NOTES.md (M0 findings), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
95 passed, 2 deselected in 0.80s   (offline suite; live tests run via probe)

.venv\Scripts\python.exe scripts\probe_deepseek.py --token-file <gitignored>
PROBE RESULT: SUCCESS (4 events; terminal MessageFinished('stop'); fixtures written)

.venv\Scripts\python.exe scripts\probe_deepseek.py --thinking --prompt "What is 2+2? Answer briefly."
PROBE RESULT: SUCCESS (38 events: 34 ReasoningDelta + TextDelta + stop)
```

### Upstream observations

- Auth/session/PoW/transport all still work against `chat.deepseek.com/api/v0` (no cookies needed for the probe account).
- Stream protocol is now event + JSON-patch with sticky-path compression (`response/content` APPEND ops, `response/status: FINISHED`, `ready` event with request/response message ids). Full details in `docs/UPSTREAM_NOTES.md`.
- Threading ids are exposed (`response_message_id`); convention for M4: next turn's `parent_message_id` = previous turn's `response_message_id`.
- No Cloudflare challenge observed; latency ~1.6–2.3 s per turn including PoW.

### Known limitations

- Synthetic fixtures predate the protocol change and now only cover the generic parser (legacy shape); live captures are ground truth.
- Error paths untriggered live; search_enabled path not probed (out of M0 scope).
- Upstream dormant 18 months — drift can recur without notice.

### Decisions added/changed

- ADR-009 vendor snapshot + relaxed curl-cffi pin
- ADR-010 UnknownDelta events
- ADR-011 M0 error surface = BackendFailure exceptions
- ADR-012 dual normalization entry points
- ADR-013 runtime stream-parser replacement (WireSession)

### Next milestone

M1 — Backend abstraction: stable `LLMBackend` interface around the spike, FakeBackend for deterministic tests, configuration boundary, and the import-boundary guard (no `dsk` imports outside `app/backends/deepseek_web`).

---

# Milestone update template

## YYYY-MM-DD — Mx: Milestone name

### Completed

### Files changed

### Tests executed

```text
command
result
```

### Upstream observations

### Known limitations

### Decisions added/changed

### Next milestone
