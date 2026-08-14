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

# Template

## ADR-XXX — Title

**Status:** Proposed / Accepted / Superseded

**Context:**

**Decision:**

**Consequences:**

**Alternatives considered:**
