# AGENTS.md

This file defines non-negotiable instructions for any coding agent working on this repository.

## Mission

Build a reliable **OpenAI-compatible gateway for DeepSeek Web** that can be used as the LLM backend for **Qwen Code coding-agent workflows**.

The gateway translates protocols. It does not execute coding tools.

## Read order

Before changing code, read:

1. `QWEN.md`
2. `00_MASTER_PROMPT.md`
2. `docs/PROJECT_BRIEF.md`
3. `docs/ARCHITECTURE.md`
4. `docs/API_CONTRACT.md`
5. `docs/QWEN_CODE_INTEGRATION.md`
5. `docs/TOOL_CALLING_PROTOCOL.md`
6. `docs/ROADMAP.md`
7. `docs/TEST_PLAN.md`
8. `docs/SECURITY.md`
9. `docs/DECISIONS.md`
10. `docs/PROGRESS.md`

## Non-negotiable rules

- Keep DeepSeek private-API behavior isolated behind `DeepSeekWebBackend`.
- Qwen Code remains the tool executor.
- Never add arbitrary server-side project shell/filesystem tools to imitate agent behavior.
- Start single-account. Multi-account comes only after Qwen Code acceptance passes.
- Admin UI comes after core agent behavior works.
- Use OpenAI Chat Completions compatibility as the first public protocol.
- Keep a normalized gateway-side conversation state.
- Do not persist raw source code, prompts, or tool output by default.
- Never log secrets.
- Add tests for every protocol transformation.
- Do not mark a milestone complete without running its acceptance checks.

## Architecture direction

Preferred package layout:

```text
app/
  main.py
  config.py
  api/
  compatibility/
  backends/
    base.py
    deepseek_web/
  conversations/
  agent/
  accounts/
  storage/
  observability/
scripts/
tests/
docs/
```

Do not force this exact layout if a better implementation becomes obvious, but preserve the architectural boundaries.

## Upstream assumptions

At the time this starter kit was authored, `deepseek4free` exposes a Python `DeepSeekAPI` that:
- authenticates using a DeepSeek Web auth token,
- creates a chat session,
- submits a prompt string,
- supports streamed chunks,
- handles PoW,
- uses cookies/Cloudflare bypass logic,
- distinguishes authentication/rate-limit/network/API errors.

These assumptions are not guarantees. Verify current upstream before implementation.

## Tool calling principle

If DeepSeek Web still lacks native tool definitions/tool calls, emulate them through the protocol in `docs/TOOL_CALLING_PROTOCOL.md`.

Never parse arbitrary JSON in normal prose as a tool call. A tool call must:
1. be inside the explicit control envelope,
2. reference a tool actually supplied by the client request,
3. have arguments that validate against the supplied schema to the practical extent possible.

## Completion discipline

After each milestone update `docs/PROGRESS.md`.

For an architectural choice that is not trivial, add a short ADR-like entry to `docs/DECISIONS.md`.

## Core acceptance target

Qwen Code must successfully solve a small codebase bug using multiple client-executed tools.

Until that works, do not spend substantial effort on UI polish.
