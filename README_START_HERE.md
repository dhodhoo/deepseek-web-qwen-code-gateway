# DeepSeek → Qwen Code Gateway Starter Pack

This package gives a coding agent the project context needed to build a local OpenAI-compatible gateway that lets **Qwen Code** use **DeepSeek Web** through the `deepseek4free` approach.

## Use it

1. Create a new project/repository.
2. Copy this whole starter pack into the root.
3. Give the coding agent `00_MASTER_PROMPT.md` as the first implementation prompt.
4. If the coding agent itself is Qwen Code, root `QWEN.md` supplies persistent project instructions.
5. Let it implement **M0 only**.
6. Review the M0 report before telling it to continue.

## Milestone order

```text
M0 DeepSeek probe
→ M1 backend abstraction
→ M2 OpenAI chat
→ M3 streaming
→ M4 conversation state
→ M5 real Qwen Code wire compatibility
→ M6 one tool call
→ M7 multi-turn tool loop
→ M8 real coding acceptance
→ reliability
→ multi-account
→ failover
→ UI
→ Docker
```

## Files

- `00_MASTER_PROMPT.md` — implementation entry prompt
- `QWEN.md` — persistent Qwen Code project instructions
- `AGENTS.md` — cross-agent rules
- `docs/PROJECT_BRIEF.md` — scope and goal
- `docs/ARCHITECTURE.md` — system boundaries
- `docs/API_CONTRACT.md` — OpenAI-compatible public API
- `docs/QWEN_CODE_INTEGRATION.md` — Qwen-specific provider/config/wire notes
- `docs/TOOL_CALLING_PROTOCOL.md` — emulated tool design
- `docs/ROADMAP.md` — milestones/exit criteria
- `docs/TEST_PLAN.md` — tests including real Qwen Code E2E
- `docs/SECURITY.md` — credential and execution boundaries
- `docs/DECISIONS.md` — lightweight ADR log
- `docs/PROGRESS.md` — updated after each milestone
- `docs/UPSTREAM_NOTES.md` — live DeepSeek/Qwen compatibility findings

## Defaults

- Python + FastAPI
- SQLite
- local/personal first
- single-account first
- OpenAI Chat Completions
- Qwen Code primary target
- Qwen Code executes coding tools
- admin UI only after core acceptance

## References to verify

- https://github.com/xtekky/deepseek4free
- https://github.com/QwenLM/qwen-code
- https://qwenlm.github.io/qwen-code-docs/en/users/configuration/model-providers/
- https://qwenlm.github.io/qwen-code-docs/en/users/configuration/auth/
- https://qwenlm.github.io/qwen-code-docs/en/users/features/memory/

Both projects evolve quickly; the coding agent must verify current behavior instead of blindly following old assumptions.
