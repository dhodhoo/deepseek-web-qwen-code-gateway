# Architecture Decisions

Keep historical decisions. Mark superseded entries instead of deleting them.

## ADR-001 — Qwen Code is the primary agent client

**Status:** Accepted

**Decision:** Optimize and regression-test the OpenAI-compatible gateway primarily against current Qwen Code.

## ADR-002 — OpenAI Chat Completions first

**Status:** Accepted

**Decision:** Implement `/v1/chat/completions` first.

**Reason:** Current Qwen Code officially supports OpenAI-compatible endpoints and uses the official OpenAI Node.js SDK for its `openai` provider path.

## ADR-003 — Qwen Code executes coding tools

**Status:** Accepted

**Decision:** The gateway translates tool definitions/decisions/results; Qwen Code performs actual tool execution.

## ADR-004 — Python + FastAPI

**Status:** Accepted

**Reason:** The DeepSeek4Free integration is Python, so this minimizes cross-language complexity.

## ADR-005 — Single account first

**Status:** Accepted

**Decision:** Add multi-account routing only after real Qwen Code coding acceptance passes.

## ADR-006 — Prompt-emulated tool calling if necessary

**Status:** Accepted with verification requirement

**Decision:** If current DeepSeek Web still lacks reliable native function/tool calls, use a strict sentinel+JSON protocol, then convert it to structured OpenAI `tool_calls`.

## ADR-007 — SQLite initially

**Status:** Accepted

## ADR-008 — Root QWEN.md plus AGENTS.md

**Status:** Accepted

**Decision:** Use `QWEN.md` for Qwen Code project context and retain `AGENTS.md` for cross-agent portability.

## ADR-009 — Vendor deepseek4free at commit 4ae47bb; relax the curl-cffi pin

**Status:** Accepted

**Context:** Upstream `deepseek4free` ships no packaging metadata (`pyproject.toml`/`setup.py`), so `pip install git+...` is impossible. Its `requirements.txt` pins `curl-cffi==0.8.1b9`; that version has no wheel for Python 3.12+/3.14 on Windows and its sdist build fails because the prebuilt `libcurl-impersonate` release it downloads returns 404. The upstream repository has not been updated since 2025-02-09 (~18 months before M0), while the development machine runs Python 3.14.6.

**Decision:**

1. Vendor the upstream snapshot at commit `4ae47bbb144f33b0ba855af9d1b0206ea794e16c` under `vendor/deepseek4free/` (MIT license preserved; provenance in `vendor/deepseek4free/VENDOR_INFO.md`). Import via a `sys.path` seam in `app/backends/deepseek_web/_vendor.py`; nothing outside that backend package may import `dsk`.
2. Depend on modern `curl-cffi` (>=0.13; 0.16.0 verified). The vendored client only _warns_ on version mismatch, and everything it uses (`requests`-style API, `impersonate='chrome120'`, `iter_lines`) is present and verified in 0.16.0.
3. Apply exactly one minimal vendor patch (`[DSQG-VENDOR-PATCH]`): `pkg_resources` → `importlib.metadata` for a warning-only version check (`pkg_resources` no longer exists in setuptools >= 81 / stock 3.12+ venvs).

**Consequences:** Reproducible, reviewable upstream snapshot; private-API quirks stay isolated as required. The upstream pin's _behavioral_ assumptions (TLS fingerprint of 0.8.1b9) may differ subtly from 0.16.0's impersonation; the M0 live probe is the check for that. Any future vendor patch needs a marker + ADR note.

**Alternatives considered:** pip-from-git (impossible — no packaging); git submodule (heavier tooling, no patch capability); rewriting the client from scratch in M0 (scope violation — M0 is a spike using the existing integration).

## ADR-010 — Preserve unknown upstream delta types as UnknownDelta events

**Status:** Accepted

**Context:** `docs/ARCHITECTURE.md` suggests a fixed event inventory, but DeepSeek Web delta `type` values are private and can change (observed: `text`, `thinking`; plausible future/search types exist).

**Decision:** Unknown delta types are normalized into an explicit `UnknownDelta(kind, content)` event instead of being dropped. Callers may ignore them; the probe reports observed unknown kinds so fixtures/tests can be updated.

**Consequences:** Upstream drift becomes observable instead of silent. Small addition beyond the suggested inventory, documented in `app/backends/events.py`.

## ADR-011 — M0 surfaces backend failures as BackendFailure exceptions

**Status:** Accepted

**Context:** ARCHITECTURE.md lists `BackendError` among stream events, but M0's spike consumes the vendored synchronous generator, where mid-stream failures surface naturally as exceptions.

**Decision:** `DeepSeekWebBackend` raises `app.backends.errors.BackendFailure` (taxonomy category + retryability + optional status). The `BackendError` event class is still implemented and tested for inventory completeness; the event-vs-exception surface for async streaming is re-decided in M1/M3.

**Consequences:** Simple, testable M0. M1 must settle the canonical surface before the API layer exists.

## ADR-012 — Dual normalization entry points: raw SSE lines and reduced chunk dicts

**Status:** Accepted

**Context:** The vendored client's `_parse_chunk` reduces each SSE payload to `{content, type, finish_reason}` and discards everything else — including message/session identifiers needed later for `parent_message_id` threading (M4) and fixtures. The upstream README's threading example even relies on a `message_id` field that the vendored parser never emits.

**Decision:** The gateway implements its own faithful raw-line parser (`parse_sse_line` + `payload_to_events`) alongside normalization of the vendored reduced dicts (`chunk_dict_to_events`). The probe captures raw SSE lines through a non-invasive `_parse_chunk` wrapping seam so fixtures reflect the true wire format. Offline tests cover both paths.

**Consequences:** Fixtures and later canonical state can carry real identifiers; parser behavior matches or exceeds vendored tolerance (non-object JSON payloads tolerated, malformed JSON fatal — same as vendored). Slightly more M0 surface, bounded and tested.

## ADR-013 — Replace the vendored stream parser at runtime (WireSession)

**Status:** Accepted

**Context:** Live probing on 2026-08-14 proved DeepSeek Web changed its stream protocol: no more OpenAI-style `choices[].delta` payloads. The current wire format is an event + JSON-patch stream with sticky-path compression (`event: ready` ids, `{"p": path, "o": op, "v": value}` ops where `p`/`o` may be omitted after being set, terminal `response/status: FINISHED`). The vendored `_parse_chunk` matched zero lines of real traffic. The vendored _transport_ (HTTP request, headers, PoW, cookies, iteration loop, terminal break on `finish_reason == 'stop'`) still works.

**Decision:** Keep the vendored transport untouched; install a stateful adapter (`app/backends/deepseek_web/wire.py::WireSession`, one instance per stream turn) as the runtime `_parse_chunk` replacement via the existing instance-attribute seam (restored exactly after each turn). The adapter translates the current protocol into the backend chunk contract (`content`/`type`/`finish_reason` + `response_message_id`/`request_message_id`), mapping `FINISHED` → `finish_reason='stop'` so the vendored loop's terminal break fires. Legacy-format handling stays in `parse_sse_line`/`payload_to_events` for regression coverage of the generic parser.

**Consequences:** Minimal change surface; private-API transport stays isolated and replaceable; the adapter is fully unit-tested against sanitized live captures (parametrized over every capture, so future probes extend coverage automatically). Risk: further upstream protocol drift will surface as UPSTREAM_PROTOCOL failures or missing events — mitigated by raw-capture fixtures from every probe run.

**Alternatives considered:** patching vendored `api.py` (rejected — destroys clean diff against upstream and spreads private-API parsing across vendor code); reimplementing the whole HTTP request in gateway code (rejected for M0 — unnecessary duplication while vendored transport works; reconsider if transport also drifts).

## ADR-014 — Stable LLMBackend interface: ABC with typed value returns

**Status:** Accepted

**Context:** M1's core deliverable is a stable backend contract. ARCHITECTURE.md sketches it "conceptually similar to" a `typing.Protocol` with `create_session -> BackendSession`, `stream_turn -> Iterator[BackendEvent]`, `health_check -> BackendHealth`, noting exact signatures may evolve. The M0 spike returned bare `str`/`dict` and used a `chat_session_id` parameter.

**Decision:**

1. `app/backends/base.py` defines `LLMBackend` as an **ABC** (not a `Protocol`), with abstract `backend_type` (satisfied by a plain class attribute), `health_check() -> BackendHealth`, `create_session() -> BackendSession`, and `stream_turn(session_id, prompt, *, parent_message_id=None, thinking_enabled=False, search_enabled=False) -> Iterator[BackendEvent]`.
2. `BackendSession(session_id)` and `BackendHealth(backend_type, ready, details)` are frozen dataclasses named by ARCHITECTURE.md, replacing bare `str`/`dict` so later milestones can extend them without breaking the signature. `session_id` is opaque above the backend layer.
3. Backend-specific extras stay OUT of the contract: `DeepSeekWebBackend.stream_turn` keeps its `raw_sink` capture extension, documented as non-portable; `FakeBackend` implements exactly the interface.
4. Failures cross the interface as `BackendFailure` exceptions (reaffirming ADR-011 for the stable contract; `BackendError` events remain inventory-only until the async streaming surface is re-decided in M3).

**Consequences:** All layers above backends (API, compiler, tool emulation) program against `base.py` only; missing methods fail at instantiation; `isinstance(x, LLMBackend)` is meaningful. `DeepSeekWebBackend` renamed its first positional `chat_session_id` → `session_id` (all in-tree callers updated; probe and live tests included).

**Alternatives considered:** `typing.Protocol` (rejected — all backends live in this repo, so nominal subtyping gives fail-fast enforcement and explicit conformance without losing anything); keeping `str`/`dict` returns (rejected — no extension headroom, weaker docs/tests).

## ADR-015 — Configuration boundary: pydantic v2 + SecretStr + factory registry

**Status:** Accepted

**Context:** M1 requires a configuration boundary: one place where environment settings become gateway objects, without ever leaking the DeepSeek token (credentials rule). The default architecture mandates pydantic v2.

**Decision:**

1. `app/config.py` is the single env→settings→backend path: `GatewaySettings.from_env(env=None)` parses `GATEWAY_BACKEND` (`deepseek_web` default, or `fake`), `DEEPSEEK_AUTH_TOKEN` (required for `deepseek_web`), and optional `DSQG_COOKIES_FILE`. The token is stored as `pydantic.SecretStr` so masking in `repr`/JSON dumps is by construction (and tested). `ConfigError` (a `ValueError`) names variables only, never values.
2. `build_backend(settings) -> LLMBackend` is the backend selection registry. It imports backend implementations **lazily**, so `GATEWAY_BACKEND=fake` development and tests never pull the vendored private-API stack.
3. `FakeBackend` is a first-class selectable backend for credential-free development of later milestones (M2/M3), not only a test fixture.

**Consequences:** New backends register in exactly one function; secret hygiene is mechanical + regression-tested; settings objects are immutable. Adds `pydantic>=2` to runtime dependencies (already required by the default architecture).

**Alternatives considered:** `pydantic-settings` (rejected for now — one extra dependency for a single injectable `from_env` classmethod; reconsider if settings grow); plain dataclasses with manual masking (rejected — hand-rolled secret masking is exactly the bug class SecretStr prevents).

## ADR-016 — Import-boundary guard: AST scan with a documented tests exemption

**Status:** Accepted

**Context:** AGENTS.md/QWEN.md require that nothing outside `app/backends/deepseek_web` imports the vendored `dsk` namespace. M0 already has intentional `dsk` imports in two test modules (`test_errors.py`, `test_backend_offline.py`) — they import vendored exception classes to prove the taxonomy mapping itself.

**Decision:** `tests/test_import_boundary.py` statically scans `app/` and `scripts/` (AST-based, so lazy/local imports inside functions are caught too) and fails on any `dsk` import outside `app/backends/deepseek_web/`. `tests/` is exempt by documented design. The guard includes a self-test of its detector so refactors cannot silently neuter it.

**Consequences:** The isolation rule is mechanically enforced on every default test run instead of relying on review. Any future exemption needs an ADR note.

## ADR-017 — Gateway API auth: secure-by-default bearer key with explicit dev opt-out

**Status:** Accepted

**Context:** API_CONTRACT.md mandates `Authorization: Bearer <GATEWAY_API_KEY>` for the OpenAI surface, distinct from the DeepSeek token, and says authentication "may be configurable" for development but "secure-by-default behavior is preferred". The gateway binds locally, but a port on 127.0.0.1 is still reachable by every local process (and by LAN peers if rebound), and it proxies a valuable DeepSeek session.

**Decision:**

1. `/v1/models` and `/v1/chat/completions` require `Authorization: Bearer <DEEPSEEK_GATEWAY_API_KEY>`; comparison uses `hmac.compare_digest`; the bearer scheme prefix is case-insensitive. Failures answer 401 with OpenAI-style `code: "invalid_api_key"` (same for missing header, wrong scheme, wrong key — no enumeration hints).
2. When no key is configured the gateway **refuses to serve**: 503 `code: "GATEWAY_API_KEY_NOT_CONFIGURED"` on `/v1/*`. The only escape hatch is the explicit opt-in `GATEWAY_ALLOW_NO_AUTH=1` (development convenience, documented as such in `.env.example`).
3. `GET /health` stays unauthenticated: it reports only liveness, version, backend type/status — never secrets (contract rule "Do not expose secrets").
4. The gateway key lives in `GatewaySettings.gateway_api_key` as `SecretStr` (ADR-015), so it inherits masking in repr/dumps.

**Consequences:** A fresh checkout cannot accidentally expose an open proxying endpoint; misconfiguration fails loudly instead of silently open. Qwen Code wiring (M5) will set the key in the provider config. The constant-time compare is cheap insurance even for a local key.

**Alternatives considered:** open-by-default with optional key (rejected — inverts the contract's stated preference and the threat of a silently open proxy); per-request signed tokens/JWT (rejected — no multi-user model exists; a shared local key is the right weight class).

## ADR-018 — M2 HTTP surface: non-stream only, explicit honest rejections, lenient parsing

**Status:** Accepted

**Context:** M2's exit criterion is "curl/OpenAI-compatible client can complete plain chat". Qwen Code source verification (docs/UPSTREAM_NOTES.md) fixed several constraints: the client always sends `stream` explicitly (agent turns stream=true), may send non-standard request fields (`reasoning_effort`, `enable_thinking`, ...), never retries 400 but retries 429/5xx, and sends `max_tokens` on every turn.

**Decision:**

1. `POST /v1/chat/completions` implements NON-STREAMING plain chat only: `system`/`user`/`assistant` text messages compiled by `app/prompt_compiler.py` into one deterministic backend prompt; a fresh backend session per request (stateless; canonical conversation state is M4).
2. Not-yet-implemented capabilities are rejected loudly and specifically instead of silently degraded: `stream: true` → 501 `STREAMING_NOT_YET_SUPPORTED` (points at M3); `tools`/`tool_choice` → 400 `TOOLS_NOT_YET_SUPPORTED` (points at M6); `role=tool`, assistant `tool_calls`, or null-content assistant messages → 400 `UNSUPPORTED_MESSAGE` (points at M6). 501 was chosen for streaming despite the client retrying 5xx (retries are bounded; the status is semantically honest; by the time Qwen Code is wired in M5, M3 streaming exists, so this path is transient).
3. Request parsing is lenient: request models use pydantic `extra="allow"`, so unknown fields (sampling knobs, vendor extras) are accepted and ignored — documented in `app/openai_types.py`, per the contract's "accept but initially may ignore" rule. Sampling parameters are NOT enforced in M2 (the backend takes a single prompt; there is nothing to map them onto).
4. `model` must equal the configured alias (`GATEWAY_MODEL_ID`, default `deepseek-web`) else 404 `model_not_found`. Empty/missing `messages`/`model` → 422 via pydantic.
5. `BackendFailure` maps to OpenAI-style errors via `app/error_mapping.py` (status per contract table, `code` = stable category value). `BackendError` events are defensively converted to failures too.
6. Handlers are plain `def` (synchronous); Starlette's threadpool isolates the blocking backend from the event loop. `finish_reason` mapping: `length` → `length`, everything else → `stop` (`tool_calls` arrives in M6).

**Consequences:** The M2 surface is fully testable offline (TestClient + FakeBackend), every unimplemented feature announces its milestone, and lenient parsing means Qwen Code's real request bodies will not 422 on contact. Session-per-request spends one upstream session (and its PoW) per call until M4 adds reuse — an accepted interim cost.

**Alternatives considered:** silently returning empty `tool_calls`/stream responses (rejected — would corrupt client state invisibly); strict request validation (rejected — the verified client sends non-standard fields); holding one long-lived backend session in the server (rejected — belongs to M4 canonical state, not the M2 adapter).

## ADR-019 — M3 streaming surface: single translator, primed HTTP-status errors, honest mid-stream failure

**Status:** Accepted

**Context:** M3's exit criterion is "incremental normal text works and raw DeepSeek SSE never leaks". ADR-011/014 deferred the `BackendError`-event-vs-exception question to this milestone ("re-decided at M3 async streaming"). Qwen source verification fixed the client-side expectations: explicit `stream:true` + `stream_options.include_usage` on agent turns, chunk accumulation by standard OpenAI rules, retries keyed off HTTP status (400 never retried; 429/5xx retried), missing usage chunk tolerated.

**Decision:**

1. `app/streaming.py` is the ONLY translator between normalized events and the public SSE wire. Rendering rules: `MessageStarted` → role chunk `{"role":"assistant","content":""}` (role is force-injected into the first rendered chunk if the backend skips `MessageStarted`); `TextDelta` → `{"content": ...}`; `MessageFinished` → empty delta + mapped finish reason (`length`→`length`, else `stop`); then `data: [DONE]`. All chunks share one `chatcmpl_local_*` id, `created`, `model`.
2. **Nothing vendor-internal crosses the wire.** `ReasoningDelta`, `BackendMessageId`, `UnknownDelta` render to NOTHING in M3 (reasoning surfacing, if ever, is a later explicit decision). Upstream JSON-patch framing, ids and control events never appear in the public stream — this is the mechanical guarantee behind the exit criterion.
3. **Errors before the first byte are HTTP statuses.** The route pulls the FIRST backend event synchronously ("priming") before returning the `StreamingResponse`, so `BackendFailure` (or a primed `BackendError` event) still answers 4xx/5xx with the OpenAI error body — preserving the client's status-keyed retry semantics.
4. **Errors after headers are committed are in-stream.** A mid-stream `BackendFailure` emits `data: {"error": {message, type, code}}` and closes WITHOUT `[DONE]` (the openai SDK raises on the error event; a missing `[DONE]` unambiguously marks an incomplete stream). Re-decision per ADR-011/014: exceptions remain the canonical cross-boundary failure surface; `BackendError` events are defensively normalized into the same failure path on both sides of priming, but stay inventory-only types.
5. **No usage chunk.** DeepSeek Web exposes no token counts; the verified client tolerates absence even with `include_usage:true`. Emitting fabricated zeros would violate "never silently pretend".
6. **Threading/disconnect.** The blocking backend iterator is consumed via `starlette.concurrency.iterate_in_threadpool`; handlers stay sync `def`. On client disconnect Starlette closes the async generator; the in-flight upstream turn runs to completion in the threadpool (stateless session policy — nothing to roll back). Yielded exception instances are defensively raised. Cancellation of in-flight upstream work is M9 scope.

**Consequences:** Streaming is fully testable offline (translator units + TestClient `stream()`); raw-leakage is guarded by construction and by explicit no-leak tests; error semantics match what the Qwen Code client actually does with statuses. Degenerate turns (empty stream) still emit a well-formed role + finish + `[DONE]` sequence.

**Alternatives considered:** emitting `[DONE]` after a mid-stream error (rejected — `[DONE]` must mean "completed successfully"); fabricating a usage chunk (rejected — dishonest data); async route handlers (rejected — the backend is blocking; threadpool isolation is the established pattern); buffering the whole turn then emitting one chunk (rejected — defeats incremental delivery).

# Template

## ADR-XXX — Title

**Status:** Proposed / Accepted / Superseded

**Context:**

**Decision:**

**Consequences:**

**Alternatives considered:**
