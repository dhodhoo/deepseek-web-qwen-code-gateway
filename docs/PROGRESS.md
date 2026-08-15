# Progress Log

The coding agent must update this file after every milestone.

## Current status

**Current milestone:** M9 — Reliability hardening — COMPLETE (offline; live behavior follows deterministically from the offline pins). M0–M9 are complete; M10 (multi-account routing) awaits the user's explicit go-ahead (milestone gate).

**State:** M9 delivered the ROADMAP reliability items behind ADR-036: a BOUNDED transport-retry policy (budget 2, deterministic linear backoff 0.5 s / 1.0 s, taxonomy-driven — non-retryable categories make exactly ONE attempt, no hot loop; final failure re-raised unchanged so mapped statuses are identical to the no-retry path); an upstream request timeout where the vendor had `timeout=None` (annotated vendor patch `DSQG_UPSTREAM_TIMEOUT_SECONDS`, default 60 s; inactivity/stall semantics on the streaming call, total timeout on control-plane calls); strict terminal behavior (a turn without `MessageFinished` is truncation — retryable pre-byte, an error envelope WITHOUT `[DONE]` mid-stream, never a fabricated `stop`); Cloudflare normalization pinned (503 `upstream_unavailable_error`, single attempt); cancellation REJECTED by decision (the call-gate semaphore cannot be released cross-thread; bounded timeout + retry subsumes the need); and operational metrics (`app/metrics.py`, ASGI middleware, open `GET /admin/metrics`). New suite `tests/test_m9_reliability.py` (34 tests) plus six planned re-pins of pre-existing tests whose single-shot retryable failures now consume the retry budget by design. Suite 424 → 458 passed.

**Previous milestone:** M8 — Real coding acceptance — COMPLETE, **ACCEPTANCE PASSED (user-run, 2026-08-15, fourth attempt after hotfix rounds 1+2)**. Autonomous read→edit→test→explain loop through the gateway (capture records 93–106). The first three attempts failed on envelope-less prose turns (marker-bearing simulation, then marker-less intent prose flushed by the termination guard) and were fixed by ADR-031/033 (round 1) and ADR-034/035 (round 2).

## Completed

- Starter architecture/specification created.
- M0 (2026-08-14): full upstream compatibility spike, including live probe.
- M1 (2026-08-14): stable backend interface, FakeBackend, configuration boundary, import-boundary guard.
- M2 (2026-08-14): FastAPI gateway surface — /health, /v1/models, non-streaming /v1/chat/completions with bearer auth, deterministic message compiler, OpenAI error mapping.
- M3 (2026-08-14): OpenAI SSE streaming — event→chunk translator, primed pre-stream error handling, in-stream mid-failure envelope, [DONE], disconnect-safe generator.
- M4 (2026-08-14): canonical conversation state — bounded in-memory store, history-prefix resolution, backend session reuse, parent-message threading, commit-on-finish + rebuild-on-failure, reconstruction tests.
- M5 (2026-08-14): real Qwen Code wire compatibility — tools[]/tool_choice accepted and ignored (plain chat usable), opt-in sanitized diagnostic capture layer, source-verified wire fixtures + fixture tests, SDK-driven wire-compat tests, Qwen Code integration/wiring doc.
- M6 (2026-08-14): one emulated tool call — lenient tools normalization, deterministic [available tools] prompt compiler, strict control-envelope parser (honest plain text on any malformed envelope), tool-shaped history compilation (assistant tool_calls + role=tool), structured OpenAI tool_calls output in both response modes, gateway-minted call_dsqg ids, canonical compact-arguments round trip.
- M7 (2026-08-15): multi-turn tool loop hardening — buffered tool turns (tool-enabled turns drained fully before any response byte in BOTH response modes; failures pre-response → HTTP status; re-emitted through the unchanged sse_stream so M6 chunk shapes are preserved), bounded repair (≤1 retry per turn with a static hint listing valid tool names — never echoed model output; re-branches on the ORIGINAL parent_message_id), backend-link invalidation after multi-attempt turns (next request rebuilds from canonical — ADR-020 self-heal), persistent tool-call ID index derived per request from canonical history, lenient tool-history validation (log-only, never rejects). ADR-028. **ACCEPTANCE PASSED (user-run, 2026-08-15, re-run after the ADR-029 hotfix):** three sequential tool interactions (`list_directory` → `read_file` ROADMAP.md → `read_file` TOOL_CALLING_PROTOCOL.md) plus a final answer built from the results, gateway executed none of the tools (capture records 66–71).
- M8 (2026-08-15, offline side): real coding acceptance preparation — ADR-030 (design-first), deterministic buggy fixture `acceptance/m8-buggy-repo/` (stdlib-only `textstats` + unittest suite, exactly one failing test / one-line fix, both states offline-verified, fixture docs never leak the bug, committed in the buggy state), offline coding-loop regression `tests/test_m8_coding_shapes.py` (five-cycle loop in the agent wire shape without tool*choice — run_shell_command → grep_search → read_file → edit with large string arguments → run_shell_command → final answer; tool-result injection boundary in both response modes). Readiness check: ZERO gateway code changes required (M6/M7 machinery is tool-agnostic). Suite 413 → 416 passed. Live acceptance is user-run. **ACCEPTANCE PASSED (user-run, 2026-08-15, FOURTH attempt):** after three failed attempts fixed by two hotfix rounds (ADR-031/033; ADR-034/035), the full loop ran live — `read_file` ×2 → `edit` → `run_shell_command` ×2 → final explanation (capture records 93–106; gateway-minted `call_dsqg*`ids round-tripping verbatim; gateway executed none of the tools); fixture suite`Ran 9 tests ... OK`; the one-line `//`→`/` fix correctly explained; fixture reset to buggy afterwards.
- M9 (2026-08-15): reliability hardening — bounded transport retry (`app/reliability.py`: budget 2, linear backoff `base × retry_number` 0.5 s / 1.0 s, no jitter; only taxonomy-retryable failures retried — RATE_LIMITED / UPSTREAM_NETWORK / UPSTREAM_5XX plus truncation; AUTH_INVALID / CLOUDFLARE_BLOCKED / UPSTREAM_PROTOCOL / CLIENT_BAD_REQUEST / INTERNAL make exactly ONE attempt; final failure re-raised unchanged → mapped statuses identical to no-retry; wraps ONLY pre-byte interactions — stream priming, buffered tool-turn drains, session creation, non-stream drains — mid-stream failures never retried), upstream timeout via annotated vendor patch (`dsk/api.py DEFAULT_REQUEST_TIMEOUT`, `DSQG_UPSTREAM_TIMEOUT_SECONDS` default 60.0; curl_cffi inactivity/stall semantics on the streaming call, total timeout otherwise), strict terminal (`_strict_terminal`: no `MessageFinished` → `UPSTREAM_PROTOCOL` truncation, retryable pre-byte; error envelope WITHOUT `[DONE]` mid-stream; zero-event turns are truncation, not empty answers), Cloudflare pins (503 `upstream_unavailable_error`, `CloudflareError` AND cloudflare-mentioning `APIError`, single attempt), cancellation explicitly REJECTED (ADR-036: call-gate Semaphore cannot release cross-thread), operational metrics (`app/metrics.py` MetricsCollector + pure-ASGI MetricsMiddleware + open `GET /admin/metrics`; request status classes + durations, backend attempts/failures/durations, transport retries, tool-turn + repair counters), six planned re-pins of pre-M9 tests, and the 34-test failure-injection suite `tests/test_m9_reliability.py`. ADR-036.

## Tests run

```text
.venv\Scripts\python.exe -m pytest -q
290 passed, 3 deselected (live tests excluded by default marker)

Post-M5 addenda (same day):
1. Repository-root .env loading for live runs (ADR-022) — user opted to
   configure via .env for the live acceptance; +4 config tests (285->289).
2. Live traffic verification fold-back: fixtures corrected to captured
   values (max_tokens 32000, temperature 0) + new traffic-shaped fixture
   side_query_respond_in_schema.json; +1 fixture test (289->290).

M5 smoke (in-process uvicorn + real openai Python SDK 3.0.0 +
GATEWAY_DIAGNOSTICS_DIR): env-parsed diagnostics_dir; health 200;
SDK non-stream chat WITH tools[] -> plain text (no fabricated
tool_calls); SDK streaming chat parsed to completion; 2 sanitized
records captured, Authorization value absent from disk.

M5 SDK wire-compat tests run a real uvicorn gateway per module and
drive it through the real openai SDK: models.list, non-stream,
streaming with stream_options.include_usage, tools tolerance,
extra_body non-standard fields — all green offline (FakeBackend).

M5 LIVE ACCEPTANCE (user-run, 2026-08-14): real Qwen Code v0.21.11
connected via .env-configured gateway; plain questions answered with
normal streamed text; 9 requests captured sanitized and structurally
diffed against tests/fixtures/qwen_code_wire/ (details in
docs/UPSTREAM_NOTES.md, "Live traffic verification").

M6 suite (same day):
.venv\Scripts\python.exe -m pytest -q
370 passed, 3 deselected (live tests excluded by default marker)
290 -> 370: new M6 suites (test_m6_tools.py, test_m6_envelope.py,
test_m6_api.py, test_m6_sdk_compat.py) plus the flipped M5-era
tool-shape tests (tool history now compiles and streams 200 instead
of 400; tools-with-plain-answer stays plain text).

M6 LIVE SMOKE (same day, real DeepSeek backend via .env): streaming
turn 1 with two declared tools finished finish_reason=tool_calls on
the FIRST TRY — id call_dsqg_57e118c6d48147efb02ad96d72b37f72, name
list_project_files, arguments {"path":"."}, zero content chars; the
non-stream round trip (re-sending that exact assistant tool_calls +
role=tool history) finished stop with content "README.md". Proof the
real model follows the control-envelope protocol and the canonical
round trip works live. Script deleted after use (established pattern).

Post-M6 hotfix (next day, four live bugs from the user's acceptance
attempt — ADR-024/025/026/027):
.venv\Scripts\python.exe -m pytest -q
370 -> 376 (instruction-block compaction tests)
     -> 379 (parent_message_id u32 conversion tests)
     -> 382 (wire snapshot-content tests)
     -> 384 passed, 3 deselected (call-gate serialization tests)
Offline replay of the captured 69-tool Qwen Code turn (diagnostics
record 13) through the real app with a FakeBackend: prompt shrank
165,262 -> 84,656 chars (instructions block 106,333 -> 25,727,
-76%), pipeline 200 + [DONE] in 0.11 s.
Live re-verification against the real backend: the same 69-tool turn
first token in 2.6 s (was >6 min stall); the tool-history turn 2 now
succeeds ON the session-reuse/delta path (1.1 s, correct answer, no
duplicate session); streamed answers arrive complete (snapshot first
chunk no longer dropped); the exact crashing pattern (side query +
agent turn fired in the same second) now completes BOTH requests and
the process survives (previously: wasmtime PoW race -> Rust panic ->
python.exe abort; crash dumps at the exact request timestamps).
Probe scripts deleted after use.

M6 LIVE ACCEPTANCE (user-run, 2026-08-15 ~07:34 local, AFTER the four
hotfixes): real Qwen Code v0.21.11 on model deepseek-web. Captures
records 32-56: the main agent turn produced tool calls; a 7-call loop
(list_directory/read_file pattern "lrllrrr", records 36-42) ran across
successive turns with results compiled back each turn; the agent even
adapted mid-loop (run_shell_command denied by the user's permission
rules -> switched to read_file). Qwen Code's background "managed
memory dream" agent ran its own tool loop through the same gateway
concurrently (serialized by the ADR-027 call gate, no crash, no
duplicate behavior). No python.exe crash dumps after the fix (last
dump 06:28:18 = pre-fix paired requests; dozens of requests since,
incl. same-second pairs, all clean). One wrinkle: the follow-up
"Baca file QWEN.md" was auto-retried 5x byte-identical by the client
before a rephrase with an @file reference succeeded via inlined
content — response-side failure mode unobservable (capture is
request-only by design); the model also answered the first question
as plain text (with a hallucinated shell block) instead of an
envelope. Both are M7 repair/instrumentation territory, not gateway
regressions.

M7 suite (2026-08-15):
.venv\Scripts\python.exe -m pytest -q
384 -> 410 passed, 3 deselected (live tests excluded by default
marker). New: tests/test_m7_loop.py (26 tests) — tool_call_index
(first-occurrence-wins), lenient validate_tool_history, the parser's
invalid_envelope_seen flag, bounded repair policy (required/optional
triggers, one-retry cap, static hint never echoing model output,
re-branch on the original parent_message_id, link invalidation +
canonical rebuild on the NEXT request, repair-failure -> HTTP 429
pre-response), buffered streaming tool turns (M6 chunk shapes
preserved, repair emits only the final outcome, streaming failure ->
502 before any byte), the 3-cycle loop through the real app with
FakeBackend (3 sequential tool interactions + final answer, ids
verbatim, delta prompts resolving tool names/results, canonical
history 8 messages), lenient history logging (orphan ids, never
content). Two M5-era fixtures + the M6 unknown-tool test updated to
repair-then-fallback semantics (scripted plain/malformed models now
cost exactly two backend calls).

M7 LIVE PROBE (2026-08-15, real DeepSeek backend through the full
pipeline, stream=true, probe script deleted after use): THREE
sequential tool interactions on the FIRST TRY — list_directory(docs)
-> read_file(docs/ROADMAP.md) -> read_file(docs/TOOL_CALLING_PROTOCOL.md)
-> final answer (stop); 13.5 s total; three unique gateway-minted
call_dsqg_ ids round-tripping verbatim through role=tool.tool_call_id;
every tool turn finish_reason=tool_calls; every continuation on the
session-reuse/delta path (one backend session). The model followed the
envelope protocol first-try each turn, so bounded repair was NOT
triggered live — it cannot be forced reliably against a cooperating
model and stays covered offline. The probe's sentinel "leak" flag was
a false positive by design: the task asked the model to QUOTE the two
sentinel strings, so their appearance in the final (stop) answer is
legitimate content; on tool_calls turns a leak is structurally
impossible (the parser consumes the envelope).

Post-M7 hotfix ADR-029 (same day, after the user's first acceptance
attempt stalled on turn 1):
.venv\Scripts\python.exe -m pytest -q
410 -> 413 passed, 3 deselected. New/changed: pre-loop plain-text
repair tests (pre-loop plain -> one retry + anti-simulation hint ->
bounded honest fallback; pre-loop plain then envelope -> tool_calls on
retry; mid-loop plain -> NO repair, link intact), optional-branch
anti-simulation wording tests; nine M5/M6-era single-turn fixtures
re-scripted to the repair-then-honest-text semantics (two backend
calls per pre-loop plain turn).
Live re-verification (real backend, replay of the EXACT captured
acceptance request — diagnostics record 61, 69 tools, tool_choice
absent, stream=true): finish_reason=tool_calls with a real
list_directory call (arguments {"path":"D:\\deepseek-agent-gateway-starter\\docs"}),
HTTP 200 in 2.6 s, no sentinels in content. Before the fix the same
request returned stop / zero tool_calls / 2,349 chars of simulated
prose. Script deleted after use (established pattern).

M7 LIVE ACCEPTANCE (user-run, 2026-08-15, AFTER the ADR-029 hotfix):
real Qwen Code on model deepseek-web, fresh session, the checklist
task (list docs/ + read ROADMAP.md and TOOL_CALLING_PROTOCOL.md +
answer from them). Capture records 66-71: turn 1 (69 tools, pre-loop)
-> tool_calls list_directory; the loop continued with the client
re-sending full history each turn and the gateway answering
read_file(ROADMAP.md), then read_file(TOOL_CALLING_PROTOCOL.md), then
the final stop answer — three sequential tool interactions, three
unique call_dsqg_ ids round-tripping verbatim through
role=tool.tool_call_id, gateway executed none of the tools. The final
answer quoted the M7 exit criterion and both sentinels correctly —
content only obtainable from the two files actually read. Wire notes:
the client shrank its tools[] from 69 to 20 after turn 1 (tools are
re-normalized per request; the id index derives from history, so this
is harmless), injected an extra user-role reminder mid-loop (compiles
as-is, lenient), and fired one tools=0 non-stream side request after
the final answer. First-try pass: no repair stall was visible in the
UI (capture is request-only, so the per-turn call count is not
recorded). M7 EXIT per ROADMAP: met.

M8 OFFLINE (2026-08-15, same day): fixture both-states verification via
stdlib unittest — buggy state: Ran 9 tests, FAILED (failures=1) with
exactly one AssertionError (1 != 1.5, test_fractional_average); fixed
state (floor division // -> true division /): Ran 9 tests, OK; fixture
restored to the buggy state for the live run. New offline regression
tests/test_m8_coding_shapes.py: five-cycle coding loop in the verified
agent wire shape (tools present, tool_choice ABSENT — run_shell_command
-> grep_search -> read_file -> edit with large string arguments ->
run_shell_command -> final explanation; ids persist verbatim through the
per-request index; six inferences, one session, no repair), plus the
tool-result injection boundary in BOTH response modes (a role=tool result
carrying a full control envelope compiles as data and never fabricates a
tool call; mid-loop plain text stays repair-free). Full offline suite:
416 passed, 3 deselected (was 413).

Post-M8 hotfix (same day, after the user's TWO failed acceptance
attempts — ADR-031 -> ADR-032 (superseded) -> ADR-033):
.venv\Scripts\python.exe -m pytest -q
416 -> 422 passed, 3 deselected. New: mid-loop simulation repair tests
(simulation marker -> bounded retry -> real envelope recovered on the
STRIPPED retry base; simulation twice -> bounded honest flush;
marker-less mid-loop text -> still NO repair; streaming surface
recovers the same way; output-only marker scope — internal markers
inside a tool RESULT never trigger repair) + the SIMULATION_MARKERS
contract. The M7 repair assertions stay untouched (non-simulation
retries keep the original prompt verbatim).
Live re-verification (real backend, replay of the EXACT captured
turn-2 request — diagnostics record 75): BEFORE the fix both attempts
returned stop / zero tool_calls / simulated loops in the internal
format; after ADR-033 the same request returns finish_reason=tool_calls
with one real read_file call, HTTP 200 in 11.4 s, zero content chars,
gateway log showing `tool repair retry 1/1 (triggers: simulation_marker)`
then the recovery. The intermediate ADR-032 was live-falsified (the
retry copied the ANNOTATED header verbatim), superseded the same day,
and its mechanism fully reverted — app/prompt_compiler.py is
byte-identical to the pinned M2-M7 state. Scripts deleted after use
(established pattern).

M8 ACCEPTANCE (user-run, 2026-08-15, FOURTH attempt, on the ADR-034/035
hotfix round 2): PASSED — all ROADMAP M8 exit criteria. The monotonic
success loop is capture records 99-106: read_file -> +read_file ->
+edit -> +run_shell_command -> +run_shell_command, then a tool-less
final summary turn; every tool call carried a gateway-minted call_dsqg_
id round-tripping verbatim through role=tool.tool_call_id, and the
gateway executed nothing. Gateway log during the run: `tool repair
retry 1/1 (triggers: no_envelope)` fired once, and the final
tool-enabled turn ended `after 2 attempt(s); repair budget exhausted
(triggers: no_envelope)` — attempt 2 answered plainly again, so the
honest final text flushed (ADR-035 termination by construction, live).
Zero simulation markers fired. Independently re-verified afterwards:
fixture diff exactly `return total // len(words)` -> `return total /
len(words)`, `Ran 9 tests ... OK`, fixture reset to its committed buggy
state.

M9 suite (same day, ADR-036):
.venv\Scripts\python.exe -m pytest -q
424 -> 458 passed, 3 deselected (live tests excluded by default
marker). New: tests/test_m9_reliability.py (34 tests) — with_transport_retry
unit pins (budget, linear backoff [0.5, 1.0] injected-sleep schedule,
taxonomy-driven single attempt for non-retryables, non-BackendFailure
propagation, max_retries=0, metrics accounting), public retry behavior
(retryable recovery 200 with turn_calls==2; non-retryable ONE attempt,
no hot loop; budget exhaustion keeps the exact no-retry error envelope;
backend_attempts counts session + turn attempts), strict terminal
(eventless turn recovery pre-byte; eventless-turn budget exhaustion ->
502 UPSTREAM_PROTOCOL; MID-stream truncation -> 200 + error envelope +
NO [DONE] + exactly ONE attempt), buffered tool-turn truncation budget,
Cloudflare pins (CloudflareError AND cloudflare-mentioning APIError ->
CLOUDFLARE_BLOCKED, 503 upstream_unavailable_error, single attempt),
timeout plumbing (request_timeout -> vendor DEFAULT_REQUEST_TIMEOUT
seam; None leaves the vendor default; env parsing), metrics surface
(open /admin/metrics shape; per-endpoint status classes; tool-turn +
repair counters), config parsing (defaults + env overrides + ConfigError
matrix). Six planned re-pins of pre-M9 tests whose single-shot
retryable failures now consume the retry budget by design (test_api
rate-limit mapping, test_api_streaming first-byte failure + the old
empty-turn test -> truncation contract, test_api_multi_turn pre-stream
failure invalidation, test_m7_loop repair-failure + streaming-tool-failure
statuses) — every re-pin asserts the SAME public status/type/code plus
exactly budget-many backend calls.
```

## Known limitations

- **M6 ACCEPTANCE PASSED 2026-08-15 (user-run)** — see the "Tests run" entry for the full capture evidence. Residual wrinkles observed during that run: (a) DeepSeek sometimes answers in plain text instead of emitting the envelope (the first turn produced a hallucinated shell block; one follow-up needed client retries + an @file rephrase) — ADDRESSED in M7 (ADR-028: bounded repair for `required`/malformed-envelope turns) and again in the ADR-029 hotfix after the first M7 acceptance attempt showed the nastier variant, prose that SIMULATES a whole tool loop with fabricated results (no envelope attempted at all): the tool instructions now forbid simulated tool execution explicitly, and PRE-loop envelope-less plain text gets the bounded repair retry too; (b) diagnostics capture is REQUEST-only, so response-side failure modes of a retried turn cannot be forensically reconstructed — response-side opt-in capture remains a future instrumentation candidate (not scheduled); (c) Qwen Code's background agents (memory "dream" passes) share the serialized backend — expected queueing under the ADR-027 call gate, observed working.
- Live Qwen Code acceptance PASSED 2026-08-14 (user-run): plain chat works through a real Qwen Code v0.21.11 install; captures traffic-verified the wire fixtures (docs/UPSTREAM_NOTES.md, "Live traffic verification"). The M5-era MONITOR flag "byte-identical re-submissions before success" is now EXPLAINED: first attempts failed on the parent_message_id wire bug (ADR-025, 422) and/or the gateway crashing under concurrent requests (ADR-027, wasmtime PoW race), and the client's retry then hit the rebuild path — creating the visible duplicate DeepSeek chat with the full prompt re-sent. Structured tool calling arrived in M6 (live smoke passed first try); the user's first tool-execution acceptance attempt surfaced the FOUR live bugs fixed in the post-M6 hotfix (ADR-024/025/026/027) — the acceptance step is user-run again and prepared turnkey.
- Backend calls are serialized by a process-wide call gate (ADR-027): the vendored client is not thread-safe (shared wasmtime PoW solver + per-turn parser seam), so concurrent OpenAI requests queue at the adapter boundary. A long turn delays any concurrent request until it finishes — expected for a single-account backend; before the gate, the same traffic killed the process.
- Tool calling (M6+M7+M8-hotfix, ADR-023/028/029/031/033/034/035): ONE tool call per model turn (parallel calls deferred); text after a valid envelope is discarded; tool-ENABLED turns are buffered end-to-end before any response byte (first-byte latency = full turn length; tool turns are short in practice), so their failures answer as HTTP statuses, never in-stream error envelopes. A missing/malformed envelope triggers at most ONE repair retry (≤2 backend calls per turn); since ADR-035 the retry fires on EVERY tool-enabled turn that ends without a valid envelope — marker-less mid-loop prose included (log label `no_envelope`). The old termination guard (mid-loop text presumed final, never repaired) was REMOVED after live evidence falsified its premise three times; termination stays guaranteed by construction — budget 1, and the non-simulation hint explicitly permits a plain answer, so a genuine final answer pays one extra inference and its second-attempt text flushes. Simulation-triggered retries (ADR-031 markers, extended to fake `[user]`/`[User]`/`[assistant]` transcripts by ADR-034) are still rebuilt on a STRIPPED history compilation with no imitable block template (ADR-033); every other retry keeps the full original prompt. Historical tool calls render as the instructed envelope itself (ADR-034 main path), so imitation becomes a valid tool request. Accepted cost: EVERY envelope-less text answer (greetings, background memory passes, genuine final answers) pays one extra backend call. After any multi-attempt turn the backend link is invalidated and the NEXT request rebuilds from canonical history. `tool_choice: "none"` fully disables tools; any other value with valid tools enables the envelope protocol. Bounded repair is now live-verified FOUR times (the ADR-029 and ADR-033 replays, plus the ADR-034/035 replays of the third acceptance's killer records 91 and 90 — both now recover real tool_calls).
- Live multi-turn acceptance against chat.deepseek.com: the delta+parent strategy is NOW live-verified (post-M6 hotfix ADR-025 — upstream requires `parent_message_id` as a u32 number; after the fix, tool-history turn 2 succeeds on the session-reuse path). The formal pytest live test `tests/test_live_upstream.py::test_live_multi_turn_threads_parent_message_id` has still never run (marker `live`); the probe covered the same behavior. If upstream ever rejects parent threading again, the rebuild path (fresh session + full-history prompt) remains correct and is the documented fallback.
- Qwen Code agent turns carry ~69 tools; the `[available tools]` block is compacted to fit the upstream prompt budget (ADR-024: first-line descriptions capped at 150 chars, schema `description` keys stripped). The total prompt for a full agent turn is still ~85KB (dominated by the client's own history) — if DeepSeek Web's prompt budget shrinks or history grows, history-side budgeting becomes necessary (not before).
- Conversation state is in-memory only (bounded 256, least-recently-updated eviction) and dies with the process; continuity self-heals because every request carries its own history. SQLite persistence deferred (ADR-020).
- Live error paths (429/5xx/Cloudflare) were not triggered during probing; classification is unit-tested offline only. Since M9 (ADR-036) every simulated failure has a DETERMINISTIC public behavior pinned by tests: retryable categories absorb up to the bounded budget (2 retries, linear 0.5 s/1.0 s backoff) and then surface the SAME mapped status as the no-retry path; non-retryable categories (incl. Cloudflare blocks) make exactly one attempt.
- M9 reliability boundaries (ADR-036): transport retry covers ONLY pre-byte interactions (priming, buffered drains, session creation) — once HTTP 200 is committed, failures/truncation emit the in-stream error envelope and never retry (replaying deltas would corrupt the wire); the upstream timeout is an INACTIVITY bound on the streaming call (silent socket aborts after `DSQG_UPSTREAM_TIMEOUT_SECONDS`, default 60 s; healthy long streams survive) and a total bound on control-plane calls — a slow-but-talking stream is not aborted; request cancellation is intentionally NOT supported (the call-gate Semaphore cannot be released cross-thread; a cancelled turn would deadlock the single backend slot); metrics are in-memory per process (reset on restart, no persistence by design).
- Upstream deepseek4free is dormant since 2025-02-09; its stream parser was fully obsolete (protocol changed). Further drift is possible at any time; probe captures are the early-warning mechanism.
- Sampling parameters are accepted but ignored; no usage chunk in streams (no upstream token counts; Qwen Code tolerates absence).
- Reasoning/thinking content is intentionally NOT surfaced in streams.
- Embeddings are not implemented (`/v1/embeddings` 404s; Qwen Code's embedContent hardcodes `text-embedding-ada-002` — out of core milestones).
- Multi-account, UI, Docker intentionally not started.

## Next action

**M9 COMPLETE (offline) — M0–M9 complete.** Reliability hardening shipped behind ADR-036: bounded transport retry (budget 2, deterministic linear backoff, taxonomy-driven; non-retryables single-attempt — no hot loop by construction), upstream timeout (annotated vendor patch; stall semantics on streams), strict terminal behavior (truncation is an error, never a fabricated `stop`; `[DONE]` only after a real terminal marker), Cloudflare normalization pins, and open operational metrics at `GET /admin/metrics`. Full offline suite 458 passed, 3 deselected; the six behavior-change re-pins were executed in the same commit (every re-pin keeps the public status/type/code and adds the budget proof). Live M9 behavior needs no acceptance run — the failure paths are simulated deterministically offline — but the running gateway instance predates M9 and must be RESTARTED to serve it. **Next milestone is M10 (multi-account routing) — per the milestone gate, do NOT start M10 without the user's explicit go-ahead.**

---

## 2026-08-15 — M8 ACCEPTANCE PASSED (user-run, fourth attempt)

### Result

PASS on all ROADMAP M8 exit criteria, gateway running the committed ADR-034/035 hotfix round 2 (`b8b5ba2`):

- Qwen Code autonomously inspected the fixture (`read_file` of `textstats.py` and `test_textstats.py`), edited the bug (`edit`), ran the tests via `run_shell_command` (twice), and explained the change — no human steering mid-loop.
- Final explanation correctly named the fix: integer division `//` → true division `/` in `average_word_length` (`return total // len(words)` → `return total / len(words)`).
- Fixture suite ends green: `Ran 9 tests ... OK` (independently re-run from the gateway side after the pass).
- The gateway executed NONE of the tools — capture records 93–106 show only gateway-minted `call_dsqg_` ids round-tripping verbatim through `role=tool.tool_call_id`; execution was Qwen Code's throughout.
- No raw sentinel text reached the UI; zero simulation markers fired during the pass.

### Evidence notes

- Monotonic success loop: records 99→106 (`read_file` → +`read_file` → +`edit` → +`run_shell_command` → +`run_shell_command`; record 106 is the tool-less final summary turn). Records 93–98 cover the same attempt's early phase including a side query.
- ADR-035 worked live exactly as designed: the gateway log shows `tool repair retry 1/1 (triggers: no_envelope)` during the run, and the final tool-enabled turn ended `after 2 attempt(s); repair budget exhausted (triggers: no_envelope)` — attempt 2 answered plainly again, so the honest text flushed. Termination by construction, not by a guard, under real acceptance traffic.
- The attempt-3 killer shapes (marker-less intent prose, fabricated `[User]`/`[assistant]` transcripts) never recurred.
- Post-pass: fixture reset to its committed buggy state (`git checkout -- acceptance/m8-buggy-repo`); the temporary verifier script was deleted after use (established pattern).

### Next milestone

M9 (reliability hardening) awaits the user's explicit go-ahead — milestone gate.

---

## 2026-08-15 — Post-M8 hotfix round 2: ADR-034 + ADR-035 (envelope-rendered history; universal bounded repair)

The user's THIRD acceptance attempt (2026-08-15 ~10:38) failed again, and the reconstruction from the gateway log + capture records 88–92 showed every existing mechanism working as designed while the model produced failure shapes escaping all detectors: turn 1 recovered via the pre-loop retry; turn 2 simulated, then recovered via the ADR-033 stripped retry (but re-read an already-read file — the stripped base loses context); turn 3 simulated, and the stripped retry's attempt 2 emitted MARKER-LESS intent prose plus a fabricated `[User]`/`[assistant]` transcript — no detector fired, the termination guard flushed it, the loop died.

Changes (all design-first, documented in docs/DECISIONS.md):

- ADR-034 — app/prompt_compiler.py: historical assistant tool calls now render BYTE-IDENTICAL to the instructed envelope (`<<<DSQG_TOOL_CALL>>>` + `{"name":...,"arguments":...}` + `<<<DSQG_END_TOOL_CALL>>>`), so the model's imitation drive produces VALID tool requests; `[tool result]` rendering unchanged. app/tool_envelope.py: SIMULATION_MARKERS extended with `[user]`, `[User]`, `[assistant]` (the record-91 transcript shape). app/tools.py + the repair hint: wording names envelopes-in-context as previous requests and forbids fake conversation turns. Re-pinned: tests/test_prompt_compiler.py (incl. the new parse-round-trip test), tests/test_m5_wire_fixtures.py tool_history_turn, the M5 fixture README row. LIVE: replay of record 91 → `finish_reason: tool_calls`, one real `run_shell_command` running the fixture's tests.
- ADR-035 — app/server.py `_run_buffered_tool_turn`: the termination guard is GONE; every envelope-less tool-enabled turn gets the one bounded retry while budget remains (empty trigger list logs as `no_envelope`). Termination preserved by construction: budget 1 + the non-simulation hint explicitly permitting plain answers (attempt-2 text always flushes). Simulation retries keep the stripped base; all other retries keep the full original prompt (informed calls, no duplicate reads). Re-pinned: the guard test flipped (test_m7_loop.py + test_m8_coding_shapes.py), count pins updated (M7 three-cycle 4→5, M8 five-cycle 6→7, poisoned/injection/scope 1→2 with second scripted turns, M6 API/M5 wire/SDK follow-ups). LIVE: replay of record 90 → attempt 1 prose again, log `tool repair retry 1/1 (triggers: no_envelope)`, attempt 2 a REAL `read_file` of the not-yet-read `test_textstats.py`.

```text
M8 HOTFIX ROUND 2 suite (2026-08-15):
.venv\Scripts\python.exe -m pytest -q
424 passed, 3 deselected (live tests excluded by default marker)
422 -> 424: new ADR-034 tests (history envelope parses back as a valid
tool request; transcript-marker contract + recovery; input-only marker
scope extended) alongside the ADR-035 re-pins.
```

---

## 2026-08-15 — Post-M8 hotfix: ADR-031 → ADR-032 (superseded) → ADR-033 (mid-loop simulation in the internal block format)

### Context

The user attempted the M8 acceptance twice and reported "sudah saya coba 2 kali, deepseek tetap gagal". Diagnosis used the sanitized capture (records 73–80 cover the M8 run) plus four deterministic replays of the exact turn-2 request (record 75): turn 1 (pre-loop, no tool blocks in compiled context) emitted the envelope correctly and executed one real `read_file`; the mid-loop turn 2 answered in PROSE simulating the entire remaining loop in the gateway's OWN internal compilation format — `[assistant tool call]` / `id:` / `tool:` / `arguments:` blocks followed by `[tool result]` blocks with fabricated file contents, no `<<<DSQG_TOOL_CALL>>>` envelope attempted. None of the ADR-029 triggers fired (`required` false — agent turns carry no tool_choice; `invalid_envelope_seen` false; `pre_loop` false — the history already holds a tool call), so the termination guard flushed the simulation and the loop died after one real tool interaction. Causal driver: the compiled history presents the internal block format as a few-shot template, and the model copies the most tool-like pattern in its context instead of following the envelope instructions (format leakage). The pre-loop shape — text blocks + tool instructions, NO tool blocks — is the shape that reliably emits envelopes.

### Completed

- ADR-031 (design-first): HIGH-PRECISION simulation markers — `SIMULATION_MARKERS` exported from `app/tool_envelope.py` (the control start sentinel appearing as data + the internal `[assistant tool call]` header). `app/server.py`'s buffered turn checks each attempt's ASSEMBLED flushed text (chunk-split-proof) — output ONLY, never history or tool results (injection boundary). Repair trigger extended: `required OR invalid_envelope_seen OR simulation_marker OR pre_loop`; budget, re-branching, commit-then-invalidate, honest flush unchanged. Anti-imitation wording in `app/tools.py` + reason-aware repair hint (`_tool_repair_hint(..., simulated=...)`). Operator INFO logging (`dsqg.server`; `app/main.py` adds `logging.basicConfig` because uvicorn only configures its own loggers). Termination guard pinned: marker-less mid-loop text is still never repaired.
- Live re-verification #1: detection + bounded retry PROVEN (log: `tool repair retry 1/1 (triggers: simulation_marker)`) but the model simulated on BOTH attempts → honest flush. The retry reused the same compiled prompt — the imitable blocks stayed in context.
- ADR-032 (design-first): retry-scoped ANNOTATED history compilation (`annotate_tool_history`; headers marked "(context only)"; default byte-identical; annotated base used only for simulation retries). Live re-verification #2 FALSIFIED its sufficiency: the retry copied the annotated header VERBATIM (`[assistant tool call (already executed — context only)]`) — the model imitates whatever block format its context displays, annotations included. ADR-032 marked Superseded; its mechanism fully reverted (`app/prompt_compiler.py` byte-identical to the pinned M2–M7 state — zero net diff).
- ADR-033 (design-first): the simulation-triggered retry base is now a STRIPPED compilation (`_prepare_turn` + `_strip_tool_history` in `app/server.py`) — every tool-shaped message omitted, assistant text kept; text blocks + tool instructions + reason-aware hint = the empirically reliable turn-1 shape. Every other retry keeps the original prompt verbatim (M7 repair assertions untouched). The simulated hint closing notes that earlier tool results are not repeated and a still-needed result must be re-requested with the envelope. Canonical history is never stripped — only the discarded retry-branch prompt (re-branch + link invalidation).
- Tests 416 → 422 (six new in tests/test_m8_coding_shapes.py: simulation → repair → recovery on the stripped base in both response modes, simulation twice → bounded honest flush on the stripped base, marker-less mid-loop text → NO repair, output-only marker scope, SIMULATION_MARKERS contract).
- LIVE RE-VERIFICATION #3 PASSED (script deleted after use): replay of the exact captured record-75 request → `finish_reason: tool_calls`, one real `read_file` call, HTTP 200 in 11.4 s, zero content chars; gateway log shows `tool repair retry 1/1 (triggers: simulation_marker)` then the recovery (no budget-exhaustion line). Before the hotfix the same request flushed a 5.5 KB internal-format simulation on both attempts.

### Files changed

```text
app/tool_envelope.py (SIMULATION_MARKERS export),
app/server.py (assembled-output marker detection, extended trigger,
  reason-aware repair hint, retry_base wiring through both buffered call
  sites, _strip_tool_history, operator logging),
app/tools.py (anti-imitation wording in the tool instructions),
app/main.py (logging.basicConfig for dsqg.server INFO lines),
tests/test_m8_coding_shapes.py (6 new hotfix tests),
docs/DECISIONS.md (ADR-031, ADR-032 superseded, ADR-033),
docs/TOOL_CALLING_PROTOCOL.md ("Simulated tool use" section),
docs/QWEN_CODE_INTEGRATION.md (re-run note), docs/PROGRESS.md
Note: app/prompt_compiler.py was modified by ADR-032 and fully reverted
when ADR-033 superseded it — zero net diff against the M2-M7 pin.
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
422 passed, 3 deselected (was 416; live tests excluded by default marker)
Live re-verification replay of diagnostics record 75: PASSED
(finish_reason=tool_calls, one real read_file, 11.4 s; script deleted
after use).
```

### Honest gaps

- The re-verification replayed the captured REQUEST, not Qwen Code itself — the user-run acceptance re-run remains the gate.
- A mid-loop turn whose first attempt simulates now costs two backend calls (latency only; the flush stays honest if both attempts simulate).
- If live evidence STILL shows simulation on the stripped shape, ADR-033 records the next escalation: a main-path format change rendering historical tool calls in the ENVELOPE format itself (turning imitation into correct behavior) — deferred; it breaks pinned M2–M7 fixtures and needs its own acceptance.

---

## 2026-08-15 — M8: Real coding acceptance (offline side complete)

### Context

M7 acceptance PASSED earlier the same day (capture records 66–71). The user then gave the explicit go-ahead for M8 — ROADMAP's "key milestone": real coding through the gateway on a tiny deterministic buggy fixture repository, prompt "Find and fix the bug, then run the tests and explain what changed." Exit: autonomous inspect/search → read → edit/patch → run tests → iterate if needed → final explanation, with the gateway remaining only the provider adapter.

### Completed

- Readiness check (ZERO code changes needed): the M6/M7 machinery is tool-agnostic — lenient tools normalization + 150-char description compaction (ADR-024) absorb the client's full tool surface; prompt_compiler.py renders tool results verbatim as DATA with no size cap (shell/test output scale is trivial for upstream prompt limits); the injection boundary is structural (only the current inference is envelope-parsed). pytest's `testpaths = ["tests"]` keeps the fixture's own test file out of the gateway suite.
- ADR-030 written DESIGN-FIRST (docs/DECISIONS.md): fixture committed in-repo at acceptance/m8-buggy-repo/ (versioned, resettable via `git checkout -- acceptance/m8-buggy-repo`); determinism contract (stdlib only, pure functions, fixed inputs, no clock/network/randomness); the bug = floor division in `average_word_length` (unambiguous failure signal, one-line fix, location must be LOCATED — the other three functions and remaining tests are correct); fixture docs never leak the bug; acceptance user-run with the exact ROADMAP prompt; alternatives recorded (fixture outside the repo, Node fixture, syntax-error bug, multi-bug fixture — all rejected).
- Fixture built and BOTH states offline-verified: buggy = `Ran 9 tests ... FAILED (failures=1)` with exactly one `AssertionError: 1 != 1.5` (test_fractional_average); fixed (`//` → `/`) = `Ran 9 tests ... OK`; fixture restored to the buggy state for the live run. Files: QWEN.md (fixture project instructions), README.md (benchmark description + reset procedure), textstats.py (four helpers, one bug), test_textstats.py (nine tests).
- tests/test_m8_coding_shapes.py (3 tests): five-cycle coding loop in the verified agent wire shape (tools present, tool_choice ABSENT: run_shell_command → grep_search → read_file → edit → run_shell_command → final explanation; the edit envelope carries large old/new string arguments and round-trips as compact JSON; ids persist verbatim through the per-request index; six inferences, one session, no repair); tool-result injection boundary in BOTH response modes (a role=tool result containing a full control envelope compiles as data under [tool result] and never fabricates a tool call; mid-loop plain text stays repair-free — ADR-029 termination guard).

### Status

Offline side complete: suite 416 passed, 3 deselected (was 413). Live acceptance is user-run — checklist in docs/QWEN_CODE_INTEGRATION.md ("M8 acceptance"). Expected gateway code change: ZERO; any live wrinkle becomes a hotfix ADR (post-M6/M7 pattern) with the capture + replay procedure as diagnostic.

---

## 2026-08-15 — Post-M7 hotfix: ADR-029 (prose-simulated tool use)

### Context

The user's first M7 acceptance attempt produced a "strange" answer: the agent turn (diagnostics record 61 — 69 tools, `tool_choice` absent, task "list docs/ then read ROADMAP.md and TOOL_CALLING_PROTOCOL.md") was answered in PROSE. The model narrated a simulated tool loop — "Saya akan menampilkan daftar file…" with a fabricated `docs/` listing reassembled from the QWEN.md reading list inside the system prompt, plus "read" summaries of files it never read. `finish_reason: stop`, zero `tool_calls`, no envelope attempted — reproduced by replaying the exact captured request through the real backend. Two ADR-028-era gaps combined: the optional instruction wording never forbade SIMULATING tool execution, and the repair trigger (`required` OR `invalid_envelope_seen`) cannot fire when no `tool_choice` arrives and no envelope is attempted.

### Completed

- ADR-029 written DESIGN-FIRST (docs/DECISIONS.md): two complementary deterministic decisions — anti-simulation instructions and pre-loop plain-text repair; alternatives recorded (repair-everything, heuristics, instructions-only, intent classification, echo-back — all rejected).
- app/tools.py: the optional branch of `build_tool_instructions` now demands the envelope when a tool is needed and explicitly forbids simulating/narrating tool execution in prose; the shared envelope rules gain "Never describe a tool call in prose; either output a real envelope or answer normally." (both branches). The `required` branch and the ADR-024 compaction are untouched.
- app/server.py: `_tool_repair_hint` gains one static anti-simulation sentence (still built only from client tool names — model output never re-enters prompts); the bounded-repair trigger widens to `no valid call AND (required OR invalid_envelope_seen OR pre_loop)`, where `pre_loop` = the request's canonical history holds no assistant tool call yet (`tool_call_index` empty). Mid-loop text answers are NEVER repaired — loop termination must stay possible on tool-carrying turns. The ADR-028 machinery is unchanged (re-branch on the original parent, commit-then-invalidate after multi-attempt turns, ≤2 backend calls per turn); both buffered call sites (stream + non-stream) receive `pre_loop`.
- Tests: `test_optional_plain_text_does_not_repair` replaced by three ADR-029 tests (pre-loop plain → repair → bounded honest fallback with hint-marker + anti-simulation assertions; pre-loop plain-then-envelope → `tool_calls` on retry; mid-loop plain → NO repair, one inference, link intact); `test_default_mode_allows_a_normal_answer` split, gaining `test_default_mode_forbids_simulated_tool_use`; nine M5/M6-era fixtures (test_api, test_api_streaming, test_m5_diagnostics, test_m5_wire_fixtures ×3, test_m6_api ×3, plus two prompt-shape tests hardened against silent 500s) re-scripted to the two-call repair semantics. 410 → 413 passed.
- LIVE RE-VERIFICATION PASSED (script deleted after use): replay of the exact captured record-61 request through the patched gateway against the real backend → HTTP 200 in 2.6 s, `finish_reason: tool_calls`, one real call `list_directory` with `{"path":"D:\\deepseek-agent-gateway-starter\\docs"}`, no sentinels in content. The acceptance blocker is resolved.

### Files changed

```text
app/tools.py (anti-simulation wording in build_tool_instructions),
app/server.py (anti-simulation repair hint, pre_loop trigger +
  threading through both buffered call sites),
tests/test_m7_loop.py (3 new repair tests replacing 1),
tests/test_m6_tools.py (wording assertions),
tests/test_api.py, tests/test_api_streaming.py,
tests/test_m5_diagnostics.py, tests/test_m5_wire_fixtures.py,
tests/test_m6_api.py (repair-semantics re-scripting),
docs/DECISIONS.md (ADR-029), docs/PROGRESS.md, docs/API_CONTRACT.md,
docs/QWEN_CODE_INTEGRATION.md, docs/UPSTREAM_NOTES.md,
docs/TOOL_CALLING_PROTOCOL.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
413 passed, 3 deselected (live tests excluded by default marker)
Live re-verification replay of diagnostics record 61: PASSED
(tool_calls, list_directory, 2.6 s; script deleted after use).
```

### Honest gaps

- The re-verification replayed the captured REQUEST, not Qwen Code itself — the user-run acceptance re-run remains the gate.
- Pre-loop plain answers (greetings, memory "dream" passes) now cost two backend calls; observed cost is latency only (the fallback stays honest text).
- Whether attempt 1 or the repair retry produced the live tool call is not observable from the replay output (capture is request-only); both paths are covered offline.

---

## 2026-08-15 — M7: Multi-turn tool loop

### Completed

- ADR-028 written DESIGN-FIRST (before code), five interlocking decisions; consequences filled after the offline suite + live probe.
- Buffered tool turns (app/server.py): every tool-ENABLED turn — both response modes — is drained through the envelope parser completely per attempt BEFORE any response byte; the final outcome is re-emitted as synthesized normalized events (MessageStarted / TextDelta\* / ToolCallEmitted? / MessageFinished) through the UNCHANGED `sse_stream`, so M6 chunk shapes stay byte-compatible. Every tool-turn failure now happens pre-response → real HTTP status (never an in-stream error envelope); tool-DISABLED streaming stays on the exact M3 path.
- Bounded repair policy: `MAX_TOOL_REPAIR_ATTEMPTS = 1` (≤2 backend calls per turn). Trigger: no valid call AND (`tool_choice == "required"` OR the parser's new `invalid_envelope_seen`). The retry appends a STATIC, deterministic hint (`_tool_repair_hint`) listing the client-supplied tool names — it NEVER echoes model output (injection boundary) — and re-branches on the SAME ORIGINAL `parent_message_id`, so the failed attempt never enters the threaded upstream context.
- Link invalidation after multi-attempt turns: commit the final result to canonical FIRST, then invalidate the backend link (session + parent) — the next request rebuilds from canonical history (ADR-020 self-healing). Single-attempt turns keep the M6 behavior exactly (link intact, delta reuse).
- Persistent tool-call ID index (app/conversation.py): `tool_call_index(messages)` derives `id → CanonicalToolCall` from canonical history per request (first occurrence wins) — deliberately NOT a stored registry; it survives eviction/restarts because the client re-sends full history. Backs `_prepare_turn`'s tool-name seeding and history validation.
- Lenient history validation (app/conversation.py + server handler): `validate_tool_history` reports orphan tool results and missing `tool_call_id`s; findings never reject a request (ADR-023 lenient-in) — the history compiles as-is and the server logs a minimal warning (`dsqg.server`: counts + ≤3 ids, never content).
- EnvelopeParser (app/tool_envelope.py): read-only `invalid_envelope_seen` flag — set on an invalid-region flush (feed) and a truncated-envelope flush (finalize); never by plain held-back text or after a valid emission. One fresh parser per attempt keeps it scoped to its own inference.
- New suite tests/test_m7_loop.py (26 tests): index semantics, lenient validation findings, the invalid-envelope flag across valid/plain/malformed/truncated feeds, repair policy (required + optional triggers, the one-retry cap, hint content, re-branch parent threading, link invalidation + rebuild, valid-first-try keeps the link, repair-failure → 429 pre-response), buffered streaming (M6 chunk shapes, repair emits only the final outcome, honest exhaustion, 502 pre-response on backend failure), the 3-cycle multi-turn loop through the real app (finish reasons tool_calls×3 + stop, ids verbatim, delta prompts resolve names/results, 8 canonical messages), lenient logging (caplog).
- Migrated tests: test_m5_wire_fixtures.py's two `tool_choice: "required"` fixtures and test_m6_api.py's unknown-tool test now script two backend turns and assert the repair-then-fallback semantics (hint marker present, valid tool names listed, unknown names absent).
- LIVE PROBE PASSED (script deleted after use): three sequential tool interactions on the first try through the real backend (streamed) plus a final answer — 13.5 s total, three unique `call_dsqg_` ids verbatim through `role=tool.tool_call_id`, all continuations on the session-reuse/delta path. Repair was not triggered live (the model cooperated first-try; cannot be forced reliably — covered offline).
- Docs synchronized: DECISIONS.md (ADR-028), API_CONTRACT.md (buffered tool turns, repair, error surface, link invalidation), QWEN_CODE_INTEGRATION.md (M7 capability + user-run M7 acceptance checklist), UPSTREAM_NOTES.md (M7 live observations), this file.

### Files changed

```text
app/server.py (buffered tool turns, repair hint + policy, link
  invalidation after multi-attempt turns, dispatch, lenient-validation
  logging, MAX_TOOL_REPAIR_ATTEMPTS),
app/conversation.py (tool_call_index, ToolHistoryFindings,
  validate_tool_history),
app/tool_envelope.py (invalid_envelope_seen flag),
tests/test_m7_loop.py (new),
tests/test_m5_wire_fixtures.py, tests/test_m6_api.py (repair semantics),
docs/DECISIONS.md (ADR-028), docs/API_CONTRACT.md,
docs/QWEN_CODE_INTEGRATION.md, docs/UPSTREAM_NOTES.md, docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
410 passed, 3 deselected (live tests excluded by default marker)
M7 live probe against the real DeepSeek backend: PASSED (3 sequential
tool interactions + final answer, first-try, 13.5 s; script deleted).
```

### Honest gaps

- The M7 EXIT — real Qwen Code completing ≥3 sequential tool interactions with a final answer — is user-run and has not happened yet (the probe covered the same pattern through the real backend; M6 acceptance already showed Qwen Code driving a 7-call loop).
- Bounded repair has never fired live (cooperating model); offline coverage is the evidence. If a future live run surfaces repeated plain-text answers under `tool_choice: "required"`, the repair path is the first place to look (log: two backend calls per such turn).
- The M7 live probe did not exercise Qwen Code's 69-tool agent shape (two declared tools); the M6 acceptance + post-M6 hotfix already covered that shape end-to-end.
- Live multi-turn pytest (`-m live`) still has never run (unchanged).

---

## 2026-08-14 — M6: One emulated tool call

### Completed

- Protocol + policy (docs/TOOL_CALLING_PROTOCOL.md, ADR-023 — supersedes the ADR-021 "tools never reach the backend prompt" invariant): the gateway teaches the model a control-envelope protocol (`<<<DSQG_TOOL_CALL>>>` / `<<<DSQG_END_TOOL_CALL>>>`) via a deterministic prompt block, parses ONLY the current inference output for it, and re-emits valid envelopes as OpenAI structured `tool_calls`. Never fabricate; any malformed/truncated envelope flushes as honest plain text; tool results and user quotes are DATA, never scanned.
- Tool normalization (`app/tools.py`, new): `normalize_tools` lenient on the way in (skips non-function/malformed entries, invalid names against `^[A-Za-z0-9_-]{1,64}$`, duplicates first-wins; empty result = tools disabled); `normalize_arguments_json` renders compact canonical JSON (`ensure_ascii=False`) in BOTH directions so re-sent history compares structurally equal; `arguments_compatible` shallow schema check (required keys, JSON types, bool never satisfies integer/number); `build_tool_instructions` deterministic `[available tools]` block with a `tool_choice: "required"` MUST variant.
- Control-envelope parser (`app/tool_envelope.py`, new): `EnvelopeParser.feed` holds back candidate sentinel prefixes across arbitrary chunk boundaries and emits `str` content or a `ToolCallEmitted`; strict validation (JSON object, known tool name, schema-compatible arguments); invalid envelopes flushed raw, truncated envelopes flushed at finalization, one call per turn, text after a valid call discarded. `parser=None` is an exact passthrough — every M2–M5 path stays byte-identical when tools are disabled or absent.
- Prompt compiler (`app/prompt_compiler.py`): tool-shaped history is now VALID — assistant `tool_calls` compile to `[assistant tool call]` blocks, `role=tool` (non-empty `tool_call_id` required) to `[tool result]` blocks, with tool names resolved from earlier calls in the sequence, then a `known_tool_names` seed built from the request's FULL canonical history (delta prompts exclude the assistant call but must still name its result), then `message.name`, else `"unknown"`. Null assistant content is valid only with tool_calls; malformed tool calls and null-content-without-calls still 400 with locations.
- Wire output (`app/openai_types.py`, `app/streaming.py`, `app/server.py`): `FunctionCallOut`/`ToolCallOut` types; non-stream responses carry `choices[0].message.tool_calls` with gateway-minted `call_dsqg_<uuid4.hex>` ids; streaming emits an opener chunk (index 0, id, name, empty arguments) plus an arguments chunk, and `finish_reason: "tool_calls"` overrides the backend reason when a call was emitted. Responses serialize via `exclude_none` inside a `JSONResponse`, so plain responses keep the exact M2 shape (no `tool_calls: null`) and tool turns omit null `content`. The parser wraps the backend stream BEFORE the recorder tap, so canonical history stores the emitted tool call (content None) for the M4 commit path.
- `tool_choice` semantics: `"none"` fully disables tools (no instructions, no parser — envelope text streams as plain content); `"required"` adds the MUST wording; `tools_enabled = bool(normalized_tools) and tool_choice != "none"`.
- New test suites (all offline): `test_m6_tools.py` (normalization leniency, canonical arguments, schema compatibility, instruction determinism), `test_m6_envelope.py` (valid/invalid/truncated envelopes, chunked feeds at sizes 1–13 with no sentinel leakage, honest raw flush, one-call-per-turn), `test_m6_api.py` (non-stream + streaming tool_calls shapes, id regex, finish reasons, tool_history round trip with session reuse + delta prompt, tool_choice none, malformed tools, 400 cases), `test_m6_sdk_compat.py` (real uvicorn + real openai SDK: scripted envelope turns parsed non-streamed and streamed, full round trip reusing the gateway-issued call id).
- Flipped M5-era tests: the fixtured tool-history turn now compiles and streams 200 with correct `[assistant tool call]`/`[tool result]` blocks and `[available tools]` instructions appended (was pinned 400 as the M6 target); tools-with-plain-answer stays plain text; diagnostics capture test updated for exclude_none null-content omission.
- Docs synchronized: ADR-023, API_CONTRACT.md (tools/tool_choice, tool-history acceptance, streaming tool-call chunks, tool-call response example), QWEN_CODE_INTEGRATION.md capability table + M6 acceptance checklist, fixture README status line, this file.
- LIVE SMOKE PASSED (real DeepSeek backend via `.env`, same day): streaming turn 1 with two declared tools followed the envelope FIRST TRY — `finish_reason: tool_calls`, `call_dsqg_57e118c6d48147efb02ad96d72b37f72`, `list_project_files`, `{"path":"."}`, zero content chars; the non-stream round trip with that exact tool history answered `README.md` with `finish_reason: stop`. Script deleted after use (established pattern). Remaining M6 acceptance is user-run: real Qwen Code executes one structured tool call.
- Post-M6 hotfix addendum (user acceptance attempt, fixed next session): the user's first real Qwen Code tool-execution run stalled and produced a duplicate DeepSeek chat; forensics on the diagnostics capture (records 9–15) + offline replay + live wire probes + crash-dump analysis found and fixed FOUR bugs, each with an ADR and live re-verification. (1) ADR-024: the 69-tool `[available tools]` block inflated the prompt to 165KB and stalled DeepSeek Web >6 min — `build_tool_instructions` now compacts (first-line descriptions ≤150 chars, schema `description` keys stripped; validation untouched) → 25,727 instruction chars (−76%), live first token 2.6 s. (2) ADR-025: DeepSeek Web 422s a string `parent_message_id` (requires u32) — every session-reuse delta turn failed fast and the client's retry rebuilt a fresh session (the duplicate chat); the adapter now converts numeric ids to int at the backend boundary → turn 2 succeeds on the reuse path live (first live verification of the M4 delta strategy). (3) ADR-026: the wire adapter dropped the first streamed chunk when upstream placed it inside the initial `{"v":{"response":{...}}}` snapshot — the snapshot branch now emits `content`/`thinking_content`. (4) ADR-027: the Rust panic the user reported (`crates\unwinder\src\stackwalk.rs`) came from `_wasmtime.dll` in the project venv — the vendored PoW solver shares ONE wasmtime Engine/Store across requests, so paired same-second requests (Qwen Code side query + agent turn) raced it, panicked, and ABORTED the gateway (`python.exe.*.dmp` crash dumps at the exact request timestamps); a `Semaphore(1)` call gate now serializes all `create_session`/`stream_turn` calls, live-verified (paired requests both succeed, process survives). Files: app/tools.py, app/backends/deepseek_web/backend.py, app/backends/deepseek_web/wire.py; tests: test_m6_tools.py (+6), test_backend_offline.py (+3 then +2), test_wire.py (+3); docs: DECISIONS.md (ADR-024/025/026/027), API_CONTRACT.md note, this file. Suite 370 → 384 passed, 3 deselected.

### Files changed

```text
app/tools.py, app/tool_envelope.py (new),
app/prompt_compiler.py (tool-shaped history + known_tool_names),
app/openai_types.py (FunctionCallOut/ToolCallOut/tool_calls),
app/streaming.py (tool-call chunks + finish_reason tool_calls),
app/server.py (parser wiring, recorder tool_calls, instructions,
  JSONResponse exclude_none)
tests/test_m6_tools.py, tests/test_m6_envelope.py, tests/test_m6_api.py,
  tests/test_m6_sdk_compat.py (new)
tests/test_api.py, tests/test_prompt_compiler.py,
  tests/test_api_streaming.py, tests/test_m5_wire_fixtures.py,
  tests/test_m5_diagnostics.py, tests/test_m5_sdk_compat.py (flipped/updated)
docs/DECISIONS.md (ADR-023), docs/API_CONTRACT.md (M6 sync),
docs/TOOL_CALLING_PROTOCOL.md (new protocol doc),
docs/QWEN_CODE_INTEGRATION.md (M6 capability + acceptance),
tests/fixtures/qwen_code_wire/README.md (status), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
370 passed, 3 deselected (live tests excluded by default marker)
M6 live smoke against the real DeepSeek backend: PASSED first try
(streaming tool call + non-stream tool-history round trip).
```

### Honest gaps

- ~~The final M6 acceptance — a real Qwen Code executing the structured tool call — is user-run and has not happened yet~~ — RESOLVED: acceptance PASSED 2026-08-15 (user-run; multi-call loop of list_directory/read_file through the gateway, results compiled back, answers incorporated — see the "Tests run" M6 LIVE ACCEPTANCE entry).
- One tool call per turn only; malformed envelopes are flushed honestly but NOT repaired (bounded repair, repeated cycles, persistent ids = M7 by design). The acceptance run showed exactly this wrinkle live: one turn got a plain-text answer instead of an envelope and needed client retries + an @file rephrase.
- Live multi-turn probe (`pytest -m live`) still has never run (unchanged from M5).

---

## 2026-08-14 — M5: Real Qwen Code wire compatibility

### Completed

- Policy change (ADR-021, partially supersedes ADR-018): `tools[]`/`tool_choice` are now ACCEPTED AND IGNORED — responses are plain text, tools are never echoed, no `tool_calls` are fabricated. Source verification showed every Qwen Code agent turn carries non-empty tools[]; the old `400 TOOLS_NOT_YET_SUPPORTED` made the M5 exit ("real Qwen Code can use the gateway for plain chat") unreachable. Tool-shaped HISTORY messages stay `400 UNSUPPORTED_MESSAGE` until M6 — unreachable in M5 plain chat and now pinned by a fixture test.
- Opt-in diagnostic capture layer (`app/diagnostics.py`, `GATEWAY_DIAGNOSTICS_DIR`): every authenticated `/v1/chat/completions` request is appended BEFORE validation to `<dir>/requests.jsonl` (rejected shapes are exactly what wire fixtures need). Sanitized: Authorization header value never written (presence only); only content-type/user-agent header values kept; request bodies written in full (the purpose of the layer — use a private directory). Best-effort: capture failures log and never break requests. Disabled by default; env + settings + `.env.example` wired.
- Wire fixtures (`tests/fixtures/qwen_code_wire/`, provenance README): agent turn (stream + stream_options.include_usage + realistic tools[]), side query (explicit stream:false, no tools), tool-loop history (assistant tool_calls with content:null + arguments JSON string; role=tool with content as array of text parts), non-standard extras (reasoning_effort, enable_thinking, thinking:{type:"disabled"}, chat_template_kwargs, preserve_thinking, metadata, cache_control, vl_high_resolution_images). Synthesized from the source verification in docs/UPSTREAM_NOTES.md (no live capture existed); the diagnostics layer is the path to traffic-verify them on first real connection.
- Fixture-driven tests (`tests/test_m5_wire_fixtures.py`): agent turn streams plain text + [DONE] with no tool_calls/usage chunks; backend receives only the compiled canonical prompt (tools never reach upstream); same body non-streamed; tool_choice 'required' tolerated; side query 200; tool-history 400 UNSUPPORTED_MESSAGE (deterministic M6 target); extras 200.
- SDK-driven wire-compat tests (`tests/test_m5_sdk_compat.py`): a real uvicorn gateway (module fixture) driven through the real openai Python SDK 3.0.0 over actual HTTP — models.list (provider/model selection), non-stream chat, streaming with include_usage, tools tolerance, extra_body non-standard fields. The closest offline proxy for Qwen Code's pinned openai Node SDK 5.11.0 (same wire protocol, different implementation).
- `docs/QWEN_CODE_INTEGRATION.md` rewritten as the turnkey wiring guide: source-verified settings.json provider entry (baseUrl ending /v1, envKey, generationConfig inside the entry — impermeable/atomic), env key setup, M5 capability table, diagnostics instructions, troubleshooting, live acceptance checklist (the 2-minute user-run step).
- API_CONTRACT.md synchronized (M5, ADR-021 bullets: tools tolerance, wire-format references, diagnostic capture); existing tools-400 tests in test_api.py / test_api_streaming.py inverted to accepted-and-ignored.
- End-to-end smoke (in-process uvicorn + real openai SDK + GATEWAY_DIAGNOSTICS_DIR): env parsing, health, non-stream chat with tools[], streaming chat, 2 sanitized capture records with no key value on disk — ALL PASS (script deleted after use, established pattern).
- Post-M5 addendum (same day): repository-root `.env` loading for live runs (ADR-022) — `load_env_file` in `app/config.py` (minimal KEY=VALUE parser, no new dependency, real environment always wins, repo-root path resolved from the app package), wired ONLY in `app/main.py` so tests/embedders keep full environment control; `.env.example` documents the copy-to-`.env` workflow; `docs/QWEN_CODE_INTEGRATION.md` gained a "Starting the gateway" section. Prompted by the user choosing `.env` configuration for the live acceptance run.
- LIVE ACCEPTANCE PASSED (same day, user-run): a real Qwen Code v0.21.11 (win32; x64) session answered plain questions through the gateway with normal streamed text. The diagnostics layer captured 9 requests; the structural diff confirmed every source-verified wire fact and folded three corrections back into the fixtures (max_tokens 32000, temperature 0 always sent, new `respond_in_schema` structured side-query shape — fixtured + regression-tested). Two observations flagged MONITOR in docs/UPSTREAM_NOTES.md ("Live traffic verification"): a reduced-tool parallel request lineage (origin unconfirmed), and byte-identical re-submissions before success on both user turns.

### Files changed

```text
app/diagnostics.py (new), app/server.py (tools tolerance + capture hook),
app/config.py (diagnostics_dir; post-M5: load_env_file),
app/main.py (post-M5: .env loading), .env.example (GATEWAY_DIAGNOSTICS_DIR;
post-M5: copy-to-.env + precedence note), pyproject.toml (dev extra: openai)
tests/fixtures/qwen_code_wire/{README.md, agent_turn_stream_with_tools.json,
  plain_chat_non_stream.json, tool_history_turn.json,
  non_standard_extras.json} (new; post-M5: agent fixture corrected to
  traffic values, side_query_respond_in_schema.json added)
tests/test_m5_wire_fixtures.py, tests/test_m5_diagnostics.py,
  tests/test_m5_sdk_compat.py (new; post-M5: respond_in_schema test)
tests/test_api.py, tests/test_api_streaming.py (tools tests inverted),
tests/test_config.py (post-M5: TestLoadEnvFile)
docs/DECISIONS.md (ADR-021; post-M5: ADR-022),
docs/QWEN_CODE_INTEGRATION.md (rewritten; post-M5: Starting the gateway;
  live acceptance PASSED),
docs/UPSTREAM_NOTES.md (post-M5: Live traffic verification section),
docs/API_CONTRACT.md (M5 sync), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
290 passed, 3 deselected (live tests excluded by default marker)
```

### Honest gaps

- Live multi-turn acceptance against chat.deepseek.com is still not run (the `live`-marked probe test exists; the user has not run it yet).
- Two captured traffic observations are not yet explained (see MONITOR in docs/UPSTREAM_NOTES.md, "Live traffic verification"); request-side capture cannot determine the first attempts' outcomes.

---

## 2026-08-14 — M4: Canonical conversation/session state

### Completed

- Canonical state module `app/conversation.py` (ADR-020): `CanonicalMessage` / `CanonicalToolCall` (tool-history-capable representation — tool fields exist now, populated from M6), `Conversation` with every ARCHITECTURE.md field (`conversation_id`, `backend_type`, `backend_account_id`, `backend_session_id`, `backend_parent_message_id`, timestamps, `status`, normalized history) plus JSON-serializable `to_dict`/`from_dict` (the reconstructable representation), and `ConversationStore` — bounded in-memory (default 256, least-recently-updated eviction), lock-guarded (threadpool-safe).
- Storage decision recorded per ARCHITECTURE.md: bounded in-memory only for v1 — nothing written to disk (privacy default "never persist raw prompts by default"); SQLite reconsidered with multi-account (M10). After a restart the client's re-sent history IS the reconstruction path.
- Conversation resolution from the request's own message history (no header, per contract): longest STRICT prefix match on canonical history returns the conversation + trailing delta; equal histories (duplicate re-sends) and divergent histories fall back to a new conversation compiled from the request's full history.
- Session reuse + parent threading: matched conversations with a live backend link reuse the backend session, send ONLY the delta prompt, and pass the stored `backend_parent_message_id`. The id comes from captured `BackendMessageId` events — the DeepSeek ready frame already emits it since M0 (wire.py → chunk_dict_to_events), so NO backend changes were needed; FakeBackend scripts it in tests. New/unlinked conversations rebuild: fresh backend session + full-history prompt — canonical state is always sufficient to rebuild a remote session (ARCHITECTURE failover requirement).
- Failure semantics (both response modes): history advances ONLY on `MessageFinished` via a shared `_TurnRecorder`; `BackendFailure` leaves history untouched and invalidates the backend link (session + parent), so the next request rebuilds. Partial assistant text is never stored.
- Compiler split (`app/prompt_compiler.py`): `messages_to_canonical` (validation gate) + `compile_canonical_to_prompt` (renders full histories AND per-turn deltas through one code path); `compile_messages_to_prompt` unchanged in behavior (all M2 tests green).
- `create_app(settings, backend, store)` — the store is injectable; reconstruction proven end-to-end: snapshot → dict → fresh store → follow-up request continues the SAME backend session + parent.
- Live multi-turn acceptance test written (`tests/test_live_upstream.py`, marker `live`): second turn on the same session parented under the first turn's `response_message_id`, with a context-recall assertion (word remembered across turns). Not executed this round (user declined manual testing); it is the acceptance check for the delta+parent strategy.

### Files changed

```text
app/conversation.py (new)
app/prompt_compiler.py (split: messages_to_canonical + compile_canonical_to_prompt)
app/server.py (conversation resolution, session reuse, parent threading,
               _TurnRecorder + commit/invalidate, injectable store)
tests/test_conversation.py (new), tests/test_api_multi_turn.py (new)
tests/test_prompt_compiler.py (canonical tests)
tests/test_api.py, tests/test_api_streaming.py (renamed duplicate-request tests + M4 notes)
tests/test_live_upstream.py (live multi-turn probe)
docs/DECISIONS.md (ADR-020), docs/API_CONTRACT.md (implementation status +
conversation identity), docs/PROGRESS.md
```

### Tests executed

```text
.venv\Scripts\python.exe -m pytest -q
265 passed, 3 deselected in 3.06s   (228 M0-M3 tests + 37 new M4 tests)

Live HTTP smoke (scripted FakeBackend behind real uvicorn, 127.0.0.1:8144):
  turn1 [user one]                            -> 200 'First reply.' (session fake-session-1)
  turn2 [user one, assistant ..., user two]   -> 200 'Second reply.'
  sessions_created=1 (reused); parent_message_id None -> 'resp-1'
  prompts: '[user]\none' then '[user]\ntwo' (delta only — session holds context)
  store: 1 conversation, 4 canonical messages, JSON snapshot 466 bytes
```

### Upstream observations

None new — M4 runs against `FakeBackend` only; no DeepSeek traffic. The deferred-from-M0 "live multi-turn acceptance" is now codified as `test_live_multi_turn_threads_parent_message_id` (marker `live`), ready for the next credential run.

### Known limitations

- Live multi-turn against chat.deepseek.com unverified (probe written, not run). Fallback if upstream rejects threading: the rebuild path (fresh session + full-history prompt) is already implemented as the invalidation path and is always correct.
- In-memory state dies on restart; continuity self-heals from client history (ADR-020 trade-off). Concurrent duplicate requests may create duplicate conversation rows (single-user local gateway; accepted).
- Re-sending an exactly-completed history starts a NEW conversation (duplicate ≠ continuation) — deliberate and tested.

### Decisions added/changed

- ADR-020 canonical conversation state: bounded in-memory store (the v1 storage decision ARCHITECTURE.md asked to record), history-prefix resolution, delta-vs-rebuild prompt rule, commit-on-finish + invalidate-on-failure, parent threading from `BackendMessageId`.

### Next milestone

M5 — Real Qwen Code wire compatibility: diagnostic fixtures from real Qwen Code traffic, request/response validation against the verified client behavior (awaiting explicit user approval).

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
