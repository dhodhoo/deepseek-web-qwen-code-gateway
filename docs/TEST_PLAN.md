# Test Plan

## Testing philosophy

Protocol adapters fail at boundaries.

Prioritize tests around:
- schemas,
- message compilation,
- stream parsing,
- tool envelope parsing,
- OpenAI output structure,
- state transitions,
- upstream error normalization.

Tests should run mostly offline.

## Test layers

### 1. Unit tests

#### Backend normalization
Using sanitized upstream fixtures:
- text chunk,
- reasoning/thinking chunk,
- stop chunk,
- empty line,
- malformed JSON,
- unexpected event fields.

#### Message compiler
Cases:
- system + user,
- multi-turn user/assistant,
- assistant tool call,
- tool result,
- large tool output,
- content containing sentinel-like strings.

#### Tool parser
Must cover:
- valid envelope,
- unknown tool,
- malformed JSON,
- missing end sentinel,
- extra prose before envelope,
- extra prose after envelope,
- sentinel inside code sample,
- tool output containing sentinel,
- wrong argument type,
- missing required argument,
- duplicate/ambiguous envelope.

#### OpenAI serialization
- non-stream text,
- non-stream tool call,
- SSE text chunks,
- finish reason stop,
- finish reason tool_calls,
- `[DONE]`.

### 2. Integration tests with fake backend

Create `FakeBackend` or equivalent.

Simulate:
- normal text stream,
- tool envelope split across arbitrary chunk boundaries,
- upstream authentication failure,
- rate limit,
- disconnect,
- malformed stream.

This allows the public FastAPI layer to be tested deterministically.

### 3. Live upstream smoke tests

Mark separately, for example:

```text
pytest -m live
```

Never run them by default in CI.

Required environment variables should be explicit.

Live tests:
- create session,
- simple prompt,
- multi-turn,
- optional thinking behavior,
- optional Cloudflare path.

Never print credentials.

### 4. Real Qwen Code wire-compatibility test

Before end-to-end coding acceptance, use a current Qwen Code installation and capture sanitized protocol fixtures. Verify:
- provider/model selection reaches the gateway;
- normal `messages` request shape;
- `stream=true`;
- actual `tools[]` definitions;
- `tool_choice` if present;
- assistant `tool_calls`;
- matching `role=tool` and `tool_call_id`;
- terminal `finish_reason`;
- harmless extra fields/extensions.

Turn all observed shapes into deterministic regression fixtures.

### 5. Qwen Code end-to-end coding test

This is the key acceptance test.

Create a fixture repository such as:

```text
fixtures/buggy_project/
  src/
  tests/
```

The bug must be deterministic and simple enough that tool protocol reliability, rather than model cleverness, is what is being tested.

Prompt:

```text
Find and fix the bug, then run the tests and explain what changed.
```

Record:
- tools requested,
- tool call order,
- finish outcome,
- test result,
- gateway errors.

Do not record entire proprietary source/tool output by default.

## Required adversarial tests

### Control-envelope injection in user message
User includes:

```text
<<<DSQG_TOOL_CALL>>>
...
```

It must remain user content, not be treated as a provider tool decision.

### Control-envelope injection in tool output
A file read returns text containing the sentinel.

It must remain data.

### Model requests unavailable tool
Reject/repair safely.

### Model attempts shell through prose
The gateway must not execute anything.

### Huge tool result
Use bounded compilation/context strategy; no uncontrolled log persistence.

### Interrupted stream
Return a controlled error/termination and leave conversation state consistent.

### Rate limit
When multi-account exists:
- account enters cooldown,
- new request can route elsewhere,
- no hot retry loop.

### Invalid auth
Account becomes invalid/disabled for routing until repaired.

## Performance sanity checks

No strict production SLA initially, but measure:
- time to first text chunk,
- total completion latency,
- PoW time if measurable,
- parser overhead,
- tool-call detection buffering,
- DB operation latency.

## Exit checklist for core release

- unit tests pass,
- integration tests pass,
- live simple chat passes,
- streaming passes,
- one real Qwen Code tool call passes,
- multi-turn tool loop passes,
- buggy-project acceptance passes,
- no server-side client tool execution exists,
- secrets are absent from test logs.
