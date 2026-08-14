# Master Prompt — DeepSeek Web Gateway for Qwen Code

You are the primary coding agent responsible for implementing this project from zero.

## Mandatory first action

Before production code:

1. Read **all Markdown files** in this repository, especially `QWEN.md`, `AGENTS.md`, and `docs/`.
2. Inspect the current upstream DeepSeek integration:
   - https://github.com/xtekky/deepseek4free
3. Inspect current Qwen Code source/docs:
   - https://github.com/QwenLM/qwen-code
   - https://qwenlm.github.io/qwen-code-docs/en/users/configuration/model-providers/
   - https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/
   - https://qwenlm.github.io/qwen-code-docs/en/users/features/memory/
4. Verify current Qwen Code OpenAI-compatible wire behavior, especially `tools`, `tool_calls`, `role=tool`, streaming, and finish reasons.
5. Compare current behavior with this specification. Record material differences in `docs/DECISIONS.md` and `docs/UPSTREAM_NOTES.md`.

Do not assume either fast-moving upstream has remained unchanged.

---

# Objective

Build a local-first gateway that lets **Qwen Code use DeepSeek Web as an OpenAI-compatible model provider**.

```text
Qwen Code
  │ OpenAI-compatible messages + tools + SSE
  ▼
DeepSeek Qwen Gateway
  ├─ OpenAI compatibility
  ├─ Qwen compatibility validation
  ├─ message compiler
  ├─ tool-calling emulator
  ├─ canonical conversation state
  ├─ stream translator
  ├─ error normalization
  └─ later: multi-account router + admin UI
  │
  ▼
DeepSeekWebBackend
  │
  ▼
deepseek4free / DeepSeek private Web API
```

The gateway is a **provider/protocol adapter**. It is not the coding agent.

Qwen Code must remain responsible for actual coding tools, permissions, filesystem access, shell commands, edits, MCP tools, and agent workflow.

The gateway must:

- accept OpenAI-style chat messages from Qwen Code;
- accept Qwen Code's OpenAI-style tool definitions;
- translate tools into a deterministic model instruction if DeepSeek Web lacks native tool calling;
- detect the DeepSeek model's tool decision;
- return a valid structured OpenAI `tool_calls` response;
- accept the subsequent Qwen Code `role: "tool"` message;
- preserve matching `tool_call_id` history;
- continue until a final text answer.

The gateway must **never execute Qwen Code's coding tools itself**.

---

# Verified starting assumptions about Qwen Code

At starter creation in August 2026, official Qwen Code docs indicate:

- custom/self-hosted OpenAI-compatible endpoints are supported;
- the `openai` provider path uses the official OpenAI Node.js SDK;
- providers are configured under `modelProviders` and can use a custom `baseUrl`;
- `baseUrl` should be an API root such as `http://127.0.0.1:8000/v1`;
- Qwen Code uses structured OpenAI tool calls for agent tools;
- project context can be supplied through `QWEN.md`.

Re-verify all of these against the current version before implementation.

---

# Default architecture

Use unless current evidence requires a change:

- Python 3.12+
- FastAPI + Uvicorn
- Pydantic v2
- SQLite
- SQLAlchemy 2.x or SQLModel; choose and document
- Alembic when persistent schema starts
- pytest
- `pyproject.toml`
- OpenAI Chat Completions first
- Qwen Code as primary acceptance client
- personal/local first
- single DeepSeek account first
- multi-account only after real coding acceptance
- admin UI after agent core
- Docker after core
- raw prompt/source/tool-output persistence disabled by default
- all private DeepSeek behavior isolated behind `DeepSeekWebBackend`

If upstream DeepSeek calls are blocking, do not block FastAPI's event loop; isolate them with a safe worker/thread boundary.

---

# Non-negotiable constraints

## Work milestone-by-milestone

Implement in this order:

1. M0 — raw DeepSeek compatibility spike
2. M1 — backend abstraction
3. M2 — basic OpenAI-compatible chat
4. M3 — streaming
5. M4 — canonical conversation/session state
6. M5 — real Qwen Code wire compatibility capture
7. M6 — one emulated tool call
8. M7 — multi-turn tool loop
9. M8 — real Qwen Code coding acceptance
10. M9 — reliability hardening
11. M10 — multi-account routing
12. M11 — session failover
13. M12 — admin UI
14. M13 — Docker/operator docs

A milestone is complete only when its tests/exit criteria pass.

## DeepSeek private API is unstable

Keep private paths, payloads, headers, PoW, cookies, Cloudflare handling, session IDs, event shapes, and transport quirks inside the backend adapter.

## Qwen Code executes tools

Never add generic gateway endpoints or internal helpers that directly run arbitrary user-project shell/filesystem/edit actions merely to simulate an agent.

Expected loop:

```text
Qwen Code sends tools
→ gateway emits tool_calls
→ Qwen Code executes
→ Qwen Code returns role=tool
→ gateway continues model inference
```

## Tool history correctness

Preserve this invariant:

```text
assistant(tool_calls=[call_X])
→ tool(tool_call_id=call_X)
→ next inference
```

Never emit an orphan tool call. Never lose or rewrite tool IDs carelessly.

## Streaming correctness

Normal text may stream immediately. If model output could be an internal emulated tool-control envelope, buffer enough to classify and validate it before exposing it as assistant text.

Successful text must finish coherently; successful tool use must finish coherently as a tool call. Do not emit conflicting duplicate terminal chunks.

## Canonical history

Do not treat the remote DeepSeek session as the sole source of truth. Keep normalized local conversation/tool state so it can be reconstructed later.

## Credentials

Never log DeepSeek auth tokens, cookies, `cf_clearance`, Authorization headers, gateway keys, or encryption keys. Do not persist raw code/tool output by default.

---

# Definition of core success

Plain chat is not enough.

Configure Qwen Code to use the gateway, open a tiny repository with a deterministic failing test, and ask:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Success requires a useful Qwen Code tool sequence such as:

```text
inspect/search
→ read
→ edit/patch
→ run test
→ optional additional iteration
→ final answer
```

The gateway must not execute those tools itself.

---

# Engineering discipline

For every milestone:

1. State the milestone.
2. Inspect existing code before editing.
3. Add/update tests.
4. Run relevant tests.
5. Fix failures before continuing.
6. Record non-trivial choices in `docs/DECISIONS.md`.
7. Update `docs/PROGRESS.md`.
8. Update `docs/UPSTREAM_NOTES.md` for current DeepSeek/Qwen behavior.
9. Keep `docs/API_CONTRACT.md` synchronized with actual behavior.
10. Do not silently broaden scope.

---

# First task — M0 only

Implement **M0 only**:

1. Initialize the Python project and testing/tooling.
2. Integrate current `deepseek4free` behind an initial `DeepSeekWebBackend` boundary.
3. Add `scripts/probe_deepseek.py` or equivalent.
4. Using a user-provided credential, verify:
   - client initialization;
   - session creation;
   - one prompt;
   - streamed output;
   - current finish behavior;
   - current upstream exceptions.
5. Normalize raw upstream events into stable internal event classes.
6. Create sanitized fixtures from observed events.
7. Write offline parser tests.
8. Fill `docs/UPSTREAM_NOTES.md` with current DeepSeek findings.
9. Do not implement tool calling, Qwen provider setup, multi-account, UI, or Docker yet.

When M0 is complete, **stop** and report:

- files changed;
- upstream revision inspected;
- observed request/stream behavior;
- tests and results;
- discrepancies from the starter assumptions;
- exact next milestone.

Do not continue to M1 until explicitly instructed.
