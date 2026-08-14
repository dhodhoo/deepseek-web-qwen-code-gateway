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

# Template

## ADR-XXX — Title

**Status:** Proposed / Accepted / Superseded

**Context:**

**Decision:**

**Consequences:**

**Alternatives considered:**
