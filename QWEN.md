# QWEN.md — Project Instructions

## Mission

Build a local OpenAI-compatible provider gateway that lets **Qwen Code** use DeepSeek Web through the `deepseek4free` approach as its LLM backend.

The gateway translates provider/model protocols. **Qwen Code remains the coding agent and tool executor.**

## Read before coding

Read, in order:

1. `00_MASTER_PROMPT.md`
2. `docs/PROJECT_BRIEF.md`
3. `docs/ARCHITECTURE.md`
4. `docs/API_CONTRACT.md`
5. `docs/QWEN_CODE_INTEGRATION.md`
6. `docs/TOOL_CALLING_PROTOCOL.md`
7. `docs/ROADMAP.md`
8. `docs/TEST_PLAN.md`
9. `docs/SECURITY.md`
10. `docs/DECISIONS.md`
11. `docs/PROGRESS.md`
12. `docs/UPSTREAM_NOTES.md`

## Non-negotiable rules

- Qwen Code executes filesystem, shell, edit, search, patch, MCP, and other coding tools.
- The gateway must never add arbitrary server-side tool execution merely to make agent demos work.
- Keep DeepSeek private API behavior behind `DeepSeekWebBackend`.
- Verify current `deepseek4free` and Qwen Code behavior before relying on this specification.
- Start single-account.
- Do not build the admin UI before real Qwen Code agent-tool acceptance works.
- Maintain canonical local conversation/tool history.
- Preserve every `tool_call_id` relationship correctly.
- Never log credentials/cookies/Authorization headers.
- Do not persist raw source code or tool output by default.
- Add tests at every protocol boundary.
- Stop at the current milestone unless explicitly instructed to continue.

## Primary acceptance target

With Qwen Code configured to the local gateway, this task must succeed:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Expected control flow:

```text
Qwen Code sends messages + tools
→ gateway asks DeepSeek
→ gateway emits structured OpenAI tool_calls
→ Qwen Code executes tools
→ Qwen Code sends role=tool results
→ repeat
→ final answer
```

The gateway itself does not read/write the user's repository or execute its test command.

## Public API target

First target:

```text
POST /v1/chat/completions
```

Qwen Code should configure `baseUrl` to the API root, e.g.:

```text
http://127.0.0.1:8000/v1
```

not to `/v1/chat/completions`.
