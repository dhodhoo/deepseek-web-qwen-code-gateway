# Implementation Roadmap

Complete milestones in order. Each milestone has an exit criterion.

## M0 — DeepSeek upstream compatibility spike

Build:
- project skeleton and dependencies;
- `DeepSeekWebBackend` spike;
- raw upstream probe script;
- normalized backend events;
- sanitized fixtures;
- parser unit tests;
- current DeepSeek notes.

Exit:
- one real prompt works with a valid credential;
- stream is consumed correctly;
- offline parser tests pass.

## M1 — Backend abstraction

Build:
- stable backend interface;
- internal error taxonomy;
- fake backend for tests;
- configuration boundary.

Exit:
- application code outside the adapter does not import private DeepSeek integration internals.

## M2 — Basic OpenAI Chat Completions

Build:
- FastAPI app;
- `/health`;
- `/v1/models`;
- `/v1/chat/completions`;
- request/response schemas;
- deterministic message compiler;
- non-stream response.

Exit:
- curl/OpenAI-compatible client can complete plain chat.

## M3 — OpenAI SSE streaming

Build:
- normalized event → OpenAI chunk translator;
- client disconnect handling;
- finish reason;
- `[DONE]`;
- streaming tests.

Exit:
- incremental normal text works and raw DeepSeek SSE never leaks.

## M4 — Canonical conversation/session state

Build:
- normalized message history;
- backend session mapping;
- parent-message mapping if current upstream uses it;
- tool-history-capable state representation;
- reconstruction tests.

Exit:
- multi-turn plain chat is correct and locally reconstructable.

## M5 — Real Qwen Code wire compatibility

Before implementing model-side tool emulation, connect a current Qwen Code install to the gateway/fake diagnostic layer.

Verify and fixture:
- provider/model selection;
- normal request body;
- streaming behavior;
- actual `tools[]` schema sent by Qwen Code;
- `tool_choice` behavior if present;
- assistant `tool_calls` history;
- matching `role=tool` result shape;
- streaming finish expectations;
- harmless extra fields/extensions.

Exit:
- real Qwen Code can use the gateway for plain chat;
- the exact current agent request/history format is documented and covered by tests.

## M6 — One emulated tool call

Build:
- normalize incoming tools;
- tool prompt compiler;
- strict control-envelope parser;
- name/schema validation;
- OpenAI structured `tool_calls` output;
- role=tool compilation.

Exit:
- real Qwen Code receives and executes one structured tool call successfully.

## M7 — Multi-turn tool loop

Build:
- persistent tool-call ID mapping;
- repeated tool-result/model cycles;
- streaming tool-control buffering;
- bounded repair policy;
- history validation.

Exit:
- Qwen Code completes at least three sequential tool interactions and receives a final answer;
- gateway executes none of those tools.

## M8 — Real coding acceptance

Use a tiny deterministic buggy fixture repository.

Prompt:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Exit:
- Qwen Code autonomously inspects/searches;
- reads relevant files;
- edits/patches;
- runs tests;
- iterates if needed;
- returns final explanation;
- gateway remains only the provider adapter.

This is the key milestone.

## M9 — Reliability hardening

Build:
- bounded retry policy;
- timeout/cancellation behavior;
- malformed/truncated stream handling;
- Cloudflare error normalization;
- strict terminal/finish behavior;
- metrics/logging;
- compatibility regression suite.

Exit:
- simulated failures produce deterministic public behavior;
- no hot/infinite retry loop.

## M10 — Multi-account router

Only after M8.

Build:
- account table;
- encrypted credentials;
- healthy/cooldown/invalid/disabled states;
- sticky conversation account;
- least-active/healthy routing;
- 401 invalidation;
- 429 cooldown.

Exit:
- new conversations avoid unhealthy accounts;
- healthy existing sessions stay sticky.

## M11 — Session failover

Build:
- create new remote session on another account;
- rehydrate canonical history;
- preserve tool history/IDs semantically;
- metrics marker.

Exit:
- simulated account failure can continue reconstructable context safely.

## M12 — Admin UI

Minimum:
- Dashboard;
- Accounts;
- Sessions;
- Requests/metrics;
- Settings;
- System health.

Exit:
- credentials are masked;
- account lifecycle is manageable;
- core API remains independent of UI.

## M13 — Docker/operator docs

Build:
- Dockerfile;
- Compose;
- persistent volume;
- `.env.example`;
- healthcheck;
- Qwen Code setup example;
- troubleshooting guide.

Exit:

```text
docker compose up -d
```

starts a usable gateway after credentials/config are supplied.

## Deferred

- parallel tool calls;
- OpenAI Responses API;
- additional model backends;
- PostgreSQL/Redis;
- multi-tenant auth;
- advanced analytics/billing;
- automatic browser credential harvesting.
