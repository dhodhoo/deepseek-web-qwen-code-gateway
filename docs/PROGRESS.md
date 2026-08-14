# Progress Log

The coding agent must update this file after every milestone.

## Current status

**Current milestone:** M6 — One emulated tool call

**State:** COMPLETE (offline-verified AND gateway-side live-verified: the M6 live smoke against the real DeepSeek backend produced a valid structured tool call on the first try, and the tool-history round trip answered correctly). The user's first real Qwen Code acceptance attempt surfaced three live bugs — all three are now DIAGNOSED, FIXED, and live-re-verified (post-M6 hotfix addendum below; ADR-024/025/026): the 69-tool instruction block stalled DeepSeek Web (>6 min), `parent_message_id` was serialized as a string where upstream requires a u32 number (every session-reuse turn 422'd, causing the duplicate-session symptom), and the wire adapter dropped the first streamed chunk when it arrived inside the initial snapshot event. Final acceptance step is user-run again: connect real Qwen Code and let it execute one structured tool call. M7 (multi-turn tool loop) not started — awaits explicit instruction.

## Completed

- Starter architecture/specification created.
- M0 (2026-08-14): full upstream compatibility spike, including live probe.
- M1 (2026-08-14): stable backend interface, FakeBackend, configuration boundary, import-boundary guard.
- M2 (2026-08-14): FastAPI gateway surface — /health, /v1/models, non-streaming /v1/chat/completions with bearer auth, deterministic message compiler, OpenAI error mapping.
- M3 (2026-08-14): OpenAI SSE streaming — event→chunk translator, primed pre-stream error handling, in-stream mid-failure envelope, [DONE], disconnect-safe generator.
- M4 (2026-08-14): canonical conversation state — bounded in-memory store, history-prefix resolution, backend session reuse, parent-message threading, commit-on-finish + rebuild-on-failure, reconstruction tests.
- M5 (2026-08-14): real Qwen Code wire compatibility — tools[]/tool_choice accepted and ignored (plain chat usable), opt-in sanitized diagnostic capture layer, source-verified wire fixtures + fixture tests, SDK-driven wire-compat tests, Qwen Code integration/wiring doc.
- M6 (2026-08-14): one emulated tool call — lenient tools normalization, deterministic [available tools] prompt compiler, strict control-envelope parser (honest plain text on any malformed envelope), tool-shaped history compilation (assistant tool_calls + role=tool), structured OpenAI tool_calls output in both response modes, gateway-minted call_dsqg ids, canonical compact-arguments round trip.

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

Post-M6 hotfix (next day, three live bugs from the user's acceptance
attempt — ADR-024/025/026):
.venv\Scripts\python.exe -m pytest -q
370 -> 376 (instruction-block compaction tests)
     -> 379 (parent_message_id u32 conversion tests)
     -> 382 passed, 3 deselected (wire snapshot-content tests)
Offline replay of the captured 69-tool Qwen Code turn (diagnostics
record 13) through the real app with a FakeBackend: prompt shrank
165,262 -> 84,656 chars (instructions block 106,333 -> 25,727,
-76%), pipeline 200 + [DONE] in 0.11 s.
Live re-verification against the real backend: the same 69-tool turn
first token in 2.6 s (was >6 min stall); the tool-history turn 2 now
succeeds ON the session-reuse/delta path (1.1 s, correct answer, no
duplicate session); streamed answers arrive complete (snapshot first
chunk no longer dropped). Probe scripts deleted after use.
```

## Known limitations

- Live Qwen Code acceptance PASSED 2026-08-14 (user-run): plain chat works through a real Qwen Code v0.21.11 install; captures traffic-verified the wire fixtures (docs/UPSTREAM_NOTES.md, "Live traffic verification"). The M5-era MONITOR flag "byte-identical re-submissions before success" is now EXPLAINED for tool-history turns: the first attempt 422'd on the parent_message_id wire bug (ADR-025) and the client retried — the retry/rebuild path then created the visible duplicate DeepSeek chat. Structured tool calling arrived in M6 (live smoke passed first try); the user's first tool-execution acceptance attempt surfaced the three live bugs fixed in the post-M6 hotfix (ADR-024/025/026) — the acceptance step is user-run again and prepared turnkey.
- M6 tool calling is intentionally single-shot (ADR-023): one tool call per model turn; text after a valid envelope is discarded; an invalid/truncated envelope flushes as honest plain text with NO bounded repair (re-prompting) — repair, repeated tool-result/model cycles, and persistent cross-request tool-call id mapping are all M7 scope. `tool_choice: "none"` fully disables tools; any other value with valid tools enables the envelope protocol.
- Live multi-turn acceptance against chat.deepseek.com: the delta+parent strategy is NOW live-verified (post-M6 hotfix ADR-025 — upstream requires `parent_message_id` as a u32 number; after the fix, tool-history turn 2 succeeds on the session-reuse path). The formal pytest live test `tests/test_live_upstream.py::test_live_multi_turn_threads_parent_message_id` has still never run (marker `live`); the probe covered the same behavior. If upstream ever rejects parent threading again, the rebuild path (fresh session + full-history prompt) remains correct and is the documented fallback.
- Qwen Code agent turns carry ~69 tools; the `[available tools]` block is compacted to fit the upstream prompt budget (ADR-024: first-line descriptions capped at 150 chars, schema `description` keys stripped). The total prompt for a full agent turn is still ~85KB (dominated by the client's own history) — if DeepSeek Web's prompt budget shrinks or history grows, history-side budgeting becomes necessary (not before).
- Conversation state is in-memory only (bounded 256, least-recently-updated eviction) and dies with the process; continuity self-heals because every request carries its own history. SQLite persistence deferred (ADR-020).
- Live error paths (429/5xx/Cloudflare) were not triggered during probing; classification is unit-tested offline only.
- Upstream deepseek4free is dormant since 2025-02-09; its stream parser was fully obsolete (protocol changed). Further drift is possible at any time; probe captures are the early-warning mechanism.
- Sampling parameters are accepted but ignored; no usage chunk in streams (no upstream token counts; Qwen Code tolerates absence).
- Reasoning/thinking content is intentionally NOT surfaced in streams.
- Embeddings are not implemented (`/v1/embeddings` 404s; Qwen Code's embedContent hardcodes `text-embedding-ada-002` — out of core milestones).
- Multi-turn tool loop (M7), multi-account, UI, Docker intentionally not started.

## Next action

M6 is complete and live-verified on the gateway side, including the three post-M6 hotfix bugs the user's acceptance attempt surfaced (ADR-024/025/026). Remaining M6 acceptance is user-run AGAIN: restart the gateway (`python -m app.main`, `.env` configured) and give real Qwen Code one task that needs a single tool — the gateway emits the structured call, Qwen Code executes it and sends the result back, the gateway compiles it and answers (docs/QWEN_CODE_INTEGRATION.md, "M6 acceptance"). Turn 2 no longer creates a duplicate DeepSeek chat and no longer stalls. After that, if the user approves, start M7 (multi-turn tool loop: persistent tool-call id mapping, repeated cycles, bounded repair, history validation). Do not start M7 without explicit instruction.

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
- Post-M6 hotfix addendum (user acceptance attempt, fixed next session): the user's first real Qwen Code tool-execution run stalled and produced a duplicate DeepSeek chat; forensics on the diagnostics capture (records 9–15) + offline replay + live wire probes found and fixed THREE bugs, each with an ADR and live re-verification. (1) ADR-024: the 69-tool `[available tools]` block inflated the prompt to 165KB and stalled DeepSeek Web >6 min — `build_tool_instructions` now compacts (first-line descriptions ≤150 chars, schema `description` keys stripped; validation untouched) → 25,727 instruction chars (−76%), live first token 2.6 s. (2) ADR-025: DeepSeek Web 422s a string `parent_message_id` (requires u32) — every session-reuse delta turn failed fast and the client's retry rebuilt a fresh session (the duplicate chat); the adapter now converts numeric ids to int at the backend boundary → turn 2 succeeds on the reuse path live (first live verification of the M4 delta strategy). (3) ADR-026: the wire adapter dropped the first streamed chunk when upstream placed it inside the initial `{"v":{"response":{...}}}` snapshot — the snapshot branch now emits `content`/`thinking_content`. Files: app/tools.py, app/backends/deepseek_web/backend.py, app/backends/deepseek_web/wire.py; tests: test_m6_tools.py (+6), test_backend_offline.py (+3), test_wire.py (+3); docs: DECISIONS.md (ADR-024/025/026), API_CONTRACT.md note, this file. Suite 370 → 382 passed, 3 deselected.

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

- The final M6 acceptance — a real Qwen Code executing the structured tool call — is user-run and has not happened yet (gateway side is live-proven; the client side is turnkey-prepared).
- One tool call per turn only; malformed envelopes are flushed honestly but NOT repaired (bounded repair, repeated cycles, persistent ids = M7 by design).
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
