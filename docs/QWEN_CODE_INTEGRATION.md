# Qwen Code Integration

## Current integration strategy

Qwen Code officially supports OpenAI-compatible providers. For the `openai` protocol it uses the official OpenAI Node.js SDK, so the gateway should expose a standards-correct OpenAI Chat Completions API rather than inventing a Qwen-specific HTTP protocol.

## Recommended `~/.qwen/settings.json`

Use a configuration in this shape and verify against the installed Qwen Code version:

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

Store the actual key in the environment rather than committing it:

```text
DEEPSEEK_GATEWAY_API_KEY=<local-gateway-key>
```

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

## Wire behavior that must be verified with a real Qwen Code installation

Before implementing tool emulation, capture sanitized request shapes and confirm:

- plain chat request fields;
- `stream` behavior;
- `tools[]` shape;
- whether/how `tool_choice` is sent;
- assistant `tool_calls` history;
- matching `role: "tool"` messages and `tool_call_id`;
- streaming tool-call delta shape expected by the installed version;
- finish reason behavior;
- extra request fields/provider extensions.

Record those findings in `UPSTREAM_NOTES.md` and turn them into fixtures/tests.

## Tool-history invariant

A valid OpenAI-compatible agent history is conceptually:

```text
assistant(tool_calls=[call_A])
→ tool(tool_call_id=call_A)
→ next assistant/model turn
```

Never emit orphan tool calls or lose their IDs.

## Plain-text pseudo tool calls

Qwen Code executes structured `tool_calls`; XML/JSON-looking prose in assistant `content` is not enough.

Therefore, if DeepSeek produces an internal emulated tool envelope, the gateway must parse it and return a real OpenAI `tool_calls` object.

## Streaming compatibility

Test explicitly:

- normal `finish_reason: "stop"`;
- tool `finish_reason: "tool_calls"`;
- no missing terminal finish on success;
- no duplicated conflicting terminal chunks;
- `[DONE]` termination.

## Qwen project instructions

This starter pack includes root `QWEN.md` because Qwen Code supports persistent Markdown project instructions/context.

Do not rely on it as the only source of project requirements; `00_MASTER_PROMPT.md` remains the implementation entry prompt.

## Acceptance setup

Create a tiny deterministic buggy repository and run Qwen Code against the gateway.

Prompt:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Pass only if Qwen Code itself executes the search/read/edit/test tools while the gateway only translates model decisions.

## Useful Qwen Code checks

During manual compatibility testing, verify the installed version and active provider/model using Qwen Code's current commands such as:

```text
/auth
/model
/about
```

For scripted tests, Qwen Code also supports non-interactive/headless prompting; verify current flags before automating them.
