# Qwen Code Integration

## Current integration strategy

Qwen Code officially supports OpenAI-compatible providers. For the `openai`
protocol it uses the official OpenAI Node.js SDK (pinned exactly at
`5.11.0` in Qwen Code v0.21.11), so the gateway exposes a
standards-correct OpenAI Chat Completions API rather than a Qwen-specific
HTTP protocol.

**Status (M7):** plain chat works end-to-end (live-accepted 2026-08-14, a
real Qwen Code v0.21.11 install answered through the gateway),
**prompt-emulated tool calling is implemented AND live-accepted** (ADR-023):
the gateway teaches DeepSeek a control-envelope protocol, parses the
answer, and emits real OpenAI structured `tool_calls`. **M6 acceptance
PASSED (user-run, 2026-08-15):** real Qwen Code executed a multi-call loop
of `list_directory` / `read_file` tool calls through the gateway and
answered from the results. **M7 (multi-turn tool loop hardening, ADR-028)
is IMPLEMENTED and live-probed:** buffered tool turns, one bounded repair
retry on missing/malformed envelopes, repeated tool-result/model cycles,
persistent tool-call ids, lenient history validation — the probe ran three
sequential tool interactions plus a final answer through the real backend
(first-try, 13.5 s). **Post-M7 hotfix (ADR-029) applied and
live-re-verified:** the first acceptance attempt stalled because the model
answered in prose SIMULATING a tool loop (fabricated results, no envelope);
the tool instructions now forbid simulated tool execution and the bounded
repair also fires on pre-loop envelope-less plain text — a replay of the
exact failing request now returns a real `tool_calls` response. M7
acceptance is user-run (checklist below). Four
post-M6 live bugs were fixed and live-re-verified (ADR-024/025/026/027).
The verified wire facts live in `docs/UPSTREAM_NOTES.md`; the fixtured
request shapes live in `tests/fixtures/qwen_code_wire/`; the tool protocol
lives in `docs/TOOL_CALLING_PROTOCOL.md`.

## Recommended `~/.qwen/settings.json`

Source-verified against Qwen Code v0.21.11 (commit `a669957f`):

```json
{
  "modelProviders": {
    "openai": [
      {
        "id": "deepseek-web",
        "name": "DeepSeek Web Gateway",
        "baseUrl": "http://127.0.0.1:8000/v1",
        "envKey": "DEEPSEEK_GATEWAY_API_KEY",
        "generationConfig": {
          "timeout": 120000,
          "maxRetries": 1
        }
      }
    ]
  },
  "security": {
    "auth": {
      "selectedType": "openai"
    }
  },
  "model": {
    "name": "deepseek-web"
  }
}
```

Field notes (verified from source):

- `baseUrl` must end in `/v1` — the SDK appends the resource path.
- `envKey` names the environment variable holding the key; Qwen Code sends
  it as `Authorization: Bearer <key>`. The key is the GATEWAY key, not the
  DeepSeek token. `security.auth.apiKey`/`baseUrl` are DEPRECATED in Qwen
  Code (removed since v0.10.1) — keys come from `envKey` env vars / `.env`
  / settings `env`.
- `generationConfig` is impermeable/atomic: keep the whole object inside
  the provider entry. `maxRetries: 1` keeps client-side retries bounded
  while the gateway's own retry policy is still M9 work (the SDK otherwise
  retries 429/5xx up to 3x on top of transport-level replays).
- The built-in `openai` protocol needs no `providerProtocol` entry (that
  key is only for custom protocol ids).

Store the actual key where Qwen Code can read it — either in the settings
`env` block (simplest; the file is local, never commit it):

```json
"env": {
  "DEEPSEEK_GATEWAY_API_KEY": "<local-gateway-key>"
}
```

or in the environment of the terminal that launches Qwen Code. The value
must equal the gateway's `DEEPSEEK_GATEWAY_API_KEY`.

## Starting the gateway

Copy `.env.example` to `.env` in the repository root and fill it in.
`python -m app.main` loads the repository-root `.env` at startup and
merges it UNDER the real environment — variables already set in the
environment always win (ADR-022). `.env` is gitignored.

```text
GATEWAY_BACKEND=deepseek_web      # or `fake` for a credential-free dry run
DEEPSEEK_AUTH_TOKEN=<deepseek web token>
DEEPSEEK_GATEWAY_API_KEY=<the same key as in Qwen Code settings>
GATEWAY_DIAGNOSTICS_DIR=<optional private capture directory>
```

```bash
python -m app.main
```

The gateway listens on `http://127.0.0.1:8000` by default
(`GATEWAY_HOST`/`GATEWAY_PORT`). Check `GET /health` once it is up.

## Important base URL rule

Correct:

```text
http://127.0.0.1:8000/v1
```

Incorrect:

```text
http://127.0.0.1:8000/v1/chat/completions
```

The OpenAI SDK appends the resource path.

## What to expect today (M7)

| Behavior                                                                                                              | Status                                                                                                                                                                                                                                        |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Plain chat, streaming (agent turns)                                                                                   | Works — `stream:true` + `stream_options.include_usage` accepted; no usage chunk is emitted (the client tolerates absence)                                                                                                                     |
| Plain chat, non-streaming (side queries)                                                                              | Works                                                                                                                                                                                                                                         |
| Multi-turn continuity                                                                                                 | Works — resolved from the request's own history (ADR-020); restart-safe                                                                                                                                                                       |
| `tools[]` / `tool_choice`                                                                                             | **Prompt-emulated tool calling (M6, ADR-023)** — a valid model envelope becomes structured `tool_calls`; `tool_choice: "none"` disables tools; missing/malformed envelopes get ONE bounded repair retry, then honest plain text (M7, ADR-028) |
| Assistant `tool_calls` / `role=tool` history                                                                          | **Accepted and compiled (M6)** — `[assistant tool call]` / `[tool result]` prompt blocks; malformed entries 400 with locations; orphan tool results compile as-is and log a warning (M7)                                                      |
| Tool loop                                                                                                             | **Repeated tool-result/model cycles (M7)** — one tool call per model turn; ids persist across turns via an index derived from the re-sent history; live-probed with 3 sequential interactions                                                 |
| Non-standard extras (`reasoning_effort`, `enable_thinking`, `chat_template_kwargs`, `metadata`, `cache_control`, ...) | Accepted and ignored (lenient parsing)                                                                                                                                                                                                        |
| `max_tokens` (always sent, possibly huge)                                                                             | Accepted; DeepSeek applies its own upstream limits                                                                                                                                                                                            |
| Embeddings (`/v1/embeddings`, client hardcodes `text-embedding-ada-002`)                                              | Not implemented — out of core milestones; the endpoint 404s                                                                                                                                                                                   |

## Wire verification status (M5 — traffic-verified)

The checklist from the M0-era plan is covered; since the 2026-08-14 live
acceptance run the plain-chat path is also **traffic-verified**:

- plain chat request fields, `stream` behavior, `tools[]` shape,
  `tool_choice`, assistant `tool_calls` history, `role: "tool"` shape,
  finish expectations, extra fields — **source-verified** from Qwen Code
  v0.21.11 and fixtured in `tests/fixtures/qwen_code_wire/` (see the
  fixture README for provenance); regression-covered by
  `tests/test_m5_wire_fixtures.py` and SDK-driven
  `tests/test_m5_sdk_compat.py`.
- Live capture diff (9 requests, diagnostics layer enabled): every
  source-verified fact confirmed; corrections folded back into the
  fixtures — `max_tokens` 32000, `temperature: 0` present, and a new
  request class (`respond_in_schema` structured side query:
  `tool_choice: 'required'` + single tool, answered plain text and
  tolerated). Details in `docs/UPSTREAM_NOTES.md`, "Live traffic
  verification".
- The diagnostic capture layer (`GATEWAY_DIAGNOSTICS_DIR`,
  `app/diagnostics.py`) stays available for future drift checks: every
  request is appended sanitized (Authorization value never written; bodies
  ARE written — use a private directory) to `<dir>/requests.jsonl`.

## Tool-history invariant

A valid OpenAI-compatible agent history is conceptually:

```text
assistant(tool_calls=[call_A])
→ tool(tool_call_id=call_A)
→ next assistant/model turn
```

Never emit orphan tool calls or lose their IDs. Implemented in M6
(ADR-023): the gateway mints `call_dsqg_<hex>` ids, stores the emitted
call in canonical history, and compiles re-sent tool history back to
deterministic prompt blocks. M7 (ADR-028) completed this: ids persist
across turns through an index derived per request from the re-sent
history (first occurrence wins — no server-side registry), and
`validate_tool_history` logs (never rejects) orphan results, which
compile as-is with tool name `unknown`.

## Plain-text pseudo tool calls

Qwen Code executes structured `tool_calls`; XML/JSON-looking prose in
assistant `content` is not enough.

Therefore, when DeepSeek produces an internal emulated tool envelope, the
gateway must parse it and return a real OpenAI `tool_calls` object.
Implemented in M6 (ADR-006 protocol, ADR-023 implementation): the
`<<<DSQG_TOOL_CALL>>>` envelope is validated (known name + schema-compatible
arguments) and emitted as a structured tool call; anything invalid is
flushed as honest plain text instead — see docs/TOOL_CALLING_PROTOCOL.md.

## Streaming compatibility

Test explicitly:

- normal `finish_reason: "stop"`;
- tool `finish_reason: "tool_calls"` — implemented M6;
- no missing terminal finish on success;
- no duplicated conflicting terminal chunks;
- `[DONE]` termination.

M3/M5 cover the text path (`tests/test_api_streaming.py`,
`tests/test_m5_sdk_compat.py` parse every emitted chunk through a real
OpenAI SDK); M6 covers the tool path (`tests/test_m6_api.py`,
`tests/test_m6_sdk_compat.py` — opener + arguments chunks, finish
override, honest flush of malformed envelopes). Since M7 (ADR-028),
tool-enabled turns are BUFFERED end-to-end before the first chunk: the
client sees identical chunk shapes, but first-byte latency equals the
whole turn, and tool-turn failures arrive as HTTP statuses (never
mid-stream error envelopes). `tests/test_m7_loop.py` pins the buffered
shapes and the repair path.

## Qwen project instructions

This starter pack includes root `QWEN.md` because Qwen Code supports
persistent Markdown project instructions/context.

Do not rely on it as the only source of project requirements;
`00_MASTER_PROMPT.md` remains the implementation entry prompt.

## Acceptance setup (M8, future)

Create a tiny deterministic buggy repository and run Qwen Code against the
gateway.

Prompt:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Pass only if Qwen Code itself executes the search/read/edit/test tools
while the gateway only translates model decisions.

## Useful Qwen Code checks

During manual compatibility testing, verify the installed version and
active provider/model using Qwen Code's current commands such as:

```text
/auth
/model
/about
```

For scripted tests, Qwen Code also supports non-interactive/headless
prompting; verify current flags before automating them.

## Live acceptance (M5 — PASSED 2026-08-14)

Executed exactly as prepared: `.env` filled (`GATEWAY_BACKEND=deepseek_web`,
real token, gateway key, diagnostics dir), `python -m app.main`, a new Qwen
Code session, `/model` → DeepSeek Web Gateway, plain questions. Result:
normal streamed answers; 9 sanitized captures recorded and diffed against
the fixtures (see "Wire verification status" above). Rollback is `/model`
back to the previous provider.

## Live verification (M6 — gateway-side smoke PASSED 2026-08-14)

The M6 live smoke ran the real DeepSeek backend through the full gateway
pipeline: a streaming agent-shaped request with two declared tools
finished `tool_calls` on the FIRST TRY (gateway-minted id, correct name,
compact arguments, zero content chars), and the non-stream request that
re-sent that exact tool history answered correctly with `finish_reason:
stop`. The model follows the control-envelope protocol; the canonical
round trip works live.

## M6 acceptance (user-run checklist)

**Status: PASSED (2026-08-15, user-run).** Real Qwen Code v0.21.11 on
model `deepseek-web` executed a multi-call loop of `list_directory` /
`read_file` through the gateway and answered from the results (diagnostics
capture records 32–56). The checklist below is retained as the repro
procedure.

The final M6 exit step — real Qwen Code receives and executes one
structured tool call:

1. `.env` configured as above; start `python -m app.main`; check `/health`.
2. In Qwen Code: `/model` → DeepSeek Web Gateway (same setup as the M5
   acceptance).
3. Ask for ONE small thing that needs a single tool, e.g. "list the files
   in the current directory" or "read QWEN.md and summarize it in one
   sentence". Keep it single-step: repeated tool cycles are M7.
4. Pass criteria: Qwen Code shows/executes one tool call and produces a
   final answer incorporating the result; the gateway logs/captures show
   the assistant `tool_calls` turn followed by the `role=tool` result
   being compiled (enable `GATEWAY_DIAGNOSTICS_DIR` to inspect the
   sanitized request trail).
5. If the answer comes back as plain text containing a raw
   `<<<DSQG_TOOL_CALL>>>` envelope, the model produced a malformed
   envelope and the gateway flushed it honestly (ADR-023); retry once —
   bounded repair is M7 scope.

Rollback is unchanged: `/model` back to the previous provider.

## M7 acceptance (user-run checklist)

**Status: PENDING (re-run after the ADR-029 hotfix).** The first attempt
(2026-08-15) stalled on turn 1: the model answered in prose, simulating
the tool loop with fabricated results (no tool call reached Qwen Code —
hence the "strange" answer). That exact failure mode is fixed and
live-re-verified (docs/DECISIONS.md ADR-029): anti-simulation tool
instructions plus a bounded repair retry for pre-loop envelope-less plain
text. Gateway side is ready — offline suite 413 passed and a replay of the
captured failing request returns `finish_reason: tool_calls`
(`list_directory` on docs); the earlier live probe already ran three
sequential tool interactions plus a final answer through the real backend
first-try (see docs/PROGRESS.md, "M7 LIVE PROBE").
The exit per ROADMAP M7: **Qwen Code completes at least three sequential
tool interactions and receives a final answer; the gateway executes none
of those tools.**

1. `.env` configured as above; start `python -m app.main`; check `/health`.
   A FRESH Qwen Code session is recommended (fresh conversation = clean
   tool-history trail).
2. In Qwen Code: `/model` → DeepSeek Web Gateway.
3. Give ONE multi-step task that needs at least three tools, e.g. (run
   inside this repository):

   ```text
   Tampilkan daftar file di direktori docs, lalu baca docs/ROADMAP.md dan
   docs/TOOL_CALLING_PROTOCOL.md, kemudian jawab: apa exit criterion
   milestone M7 dan apa dua sentinel control envelope-nya?
   ```

   Any task with the same shape works (list + two reads, or
   list/read/grep in any order). Do NOT pre-attach files with `@` — the
   point is that the model must request them through tools.

4. Pass criteria:
   - Qwen Code executes ≥3 sequential tool calls (each shown/confirmed in
     the UI) and then produces a final answer built from the results;
   - the gateway executes NONE of those tools — it only translates model
     decisions into `tool_calls` and compiles the results back (verify in
     `GATEWAY_DIAGNOSTICS_DIR/requests.jsonl`: alternating assistant
     `tool_calls` / `role=tool` requests, all with verbatim `call_dsqg_`
     ids);
   - no raw sentinel text (`<<<DSQG_TOOL_CALL>>>`) reaches the UI as
     assistant prose on a tool turn.
5. If a turn takes noticeably longer than M6 turns: tool-enabled turns
   are buffered end-to-end (ADR-028), and a turn that needed the bounded
   repair costs two backend calls — since ADR-029 this also applies to a
   PRE-loop turn that first answers plain text without an envelope (the
   anti-simulation retry), so a slow FIRST turn with a correct tool call
   or honest text at the end is the hotfix working, not a regression.
   A turn that ultimately answers plain text after a repair still keeps
   the session usable (the next request self-heals via canonical rebuild
   if the turn used more than one attempt).
6. If an answer ever LOOKS like simulated tool execution again ("Saya
   akan membaca file…" followed by fabricated results, no tool shown in
   the UI): that is the ADR-029 failure mode escaping both defenses —
   note the line count in `GATEWAY_DIAGNOSTICS_DIR/requests.jsonl` for
   the turn and report it (capture is request-only, so the response side
   needs the replay treatment).
