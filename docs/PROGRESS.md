# Progress Log

The coding agent must update this file after every milestone.

## Current status

**Current milestone:** M0 — Upstream compatibility spike

**State:** COMPLETE (live-verified 2026-08-14). Awaiting user review; M1 not started.

## Completed

- Starter architecture/specification created.
- M0 (2026-08-14): full upstream compatibility spike, including live probe.

## Tests run

```text
.venv\Scripts\python.exe -m pytest -q
95 passed, 2 deselected (live tests excluded by default marker)
```

## Known limitations

- Live error paths (429/5xx/Cloudflare) were not triggered during probing; classification is unit-tested offline only.
- Multi-turn threading (parent_message_id = previous response_message_id) is captured but not yet exercised end-to-end (M4).
- Upstream deepseek4free is dormant since 2025-02-09; its stream parser was fully obsolete (protocol changed). Further drift is possible at any time; probe captures are the early-warning mechanism.
- Tool calling, Qwen Code provider wiring, multi-account, UI, Docker intentionally not started.

## Next action

User reviews the M0 report. If approved, start M1 (backend abstraction: formal interface, FakeBackend, configuration boundary).

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
