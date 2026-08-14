# Qwen Code Integration

## Current integration strategy

Qwen Code officially supports OpenAI-compatible providers. For the `openai`
protocol it uses the official OpenAI Node.js SDK (pinned exactly at
`5.11.0` in Qwen Code v0.21.11), so the gateway exposes a
standards-correct OpenAI Chat Completions API rather than a Qwen-specific
HTTP protocol.

**Status (M5):** plain chat works end-to-end offline (SDK-driven wire
tests, fixture tests). Structured tool calls arrive in M6 — until then the
gateway accepts the `tools[]` Qwen Code always sends but answers plain
text (ADR-021). The verified wire facts live in
`docs/UPSTREAM_NOTES.md`; the fixtured request shapes live in
`tests/fixtures/qwen_code_wire/`.

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

## What to expect today (M5)

| Behavior                                                                                                              | Status                                                                                                                    |
| --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Plain chat, streaming (agent turns)                                                                                   | Works — `stream:true` + `stream_options.include_usage` accepted; no usage chunk is emitted (the client tolerates absence) |
| Plain chat, non-streaming (side queries)                                                                              | Works                                                                                                                     |
| Multi-turn continuity                                                                                                 | Works — resolved from the request's own history (ADR-020); restart-safe                                                   |
| `tools[]` / `tool_choice`                                                                                             | **Accepted and ignored** — answers are plain text; structured `tool_calls` arrive in M6 (ADR-021)                         |
| Assistant `tool_calls` / `role=tool` history                                                                          | `400 UNSUPPORTED_MESSAGE` until M6 — unreachable in a plain-chat session because the gateway never emits tool calls yet   |
| Non-standard extras (`reasoning_effort`, `enable_thinking`, `chat_template_kwargs`, `metadata`, `cache_control`, ...) | Accepted and ignored (lenient parsing)                                                                                    |
| `max_tokens` (always sent, possibly huge)                                                                             | Accepted; DeepSeek applies its own upstream limits                                                                        |
| Embeddings (`/v1/embeddings`, client hardcodes `text-embedding-ada-002`)                                              | Not implemented — out of core milestones; the endpoint 404s                                                               |

## Wire verification status (M5)

The checklist from the M0-era plan is now covered without live capture,
because no live Qwen Code session has run yet:

- plain chat request fields, `stream` behavior, `tools[]` shape,
  `tool_choice`, assistant `tool_calls` history, `role: "tool"` shape,
  finish expectations, extra fields — all **source-verified** from Qwen
  Code v0.21.11 and fixtured in `tests/fixtures/qwen_code_wire/` (see the
  fixture README for provenance); regression-covered by
  `tests/test_m5_wire_fixtures.py` and SDK-driven
  `tests/test_m5_sdk_compat.py`.
- When a real Qwen Code IS connected, enable the diagnostic capture layer
  (`GATEWAY_DIAGNOSTICS_DIR`, `app/diagnostics.py`): every authenticated
  request is appended sanitized (Authorization value never written; bodies
  ARE written — use a private directory) to `<dir>/requests.jsonl`.
  Compare captures against the fixtures and record drift in
  `UPSTREAM_NOTES.md`.

## Tool-history invariant

A valid OpenAI-compatible agent history is conceptually:

```text
assistant(tool_calls=[call_A])
→ tool(tool_call_id=call_A)
→ next assistant/model turn
```

Never emit orphan tool calls or lose their IDs. (M6 work; the canonical
representation already exists in `app/conversation.py`.)

## Plain-text pseudo tool calls

Qwen Code executes structured `tool_calls`; XML/JSON-looking prose in
assistant `content` is not enough.

Therefore, when DeepSeek produces an internal emulated tool envelope, the
gateway must parse it and return a real OpenAI `tool_calls` object (M6,
ADR-006).

## Streaming compatibility

Test explicitly:

- normal `finish_reason: "stop"`;
- tool `finish_reason: "tool_calls"` (M6);
- no missing terminal finish on success;
- no duplicated conflicting terminal chunks;
- `[DONE]` termination.

M3/M5 cover the text path (`tests/test_api_streaming.py`,
`tests/test_m5_sdk_compat.py` parse every emitted chunk through a real
OpenAI SDK).

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

## Live acceptance (M5, prepared, user-run)

Everything above is verified offline. The remaining two-minute live step:

1. Fill the gateway's `.env` (`GATEWAY_BACKEND=deepseek_web`, real
   `DEEPSEEK_AUTH_TOKEN`, `DEEPSEEK_GATEWAY_API_KEY`, optionally
   `GATEWAY_DIAGNOSTICS_DIR`) and start `python -m app.main`.
2. Start Qwen Code with the settings above and ask one plain question.

Expected: a normal streamed answer, and one sanitized record per request
in the diagnostics directory when capture is enabled.
