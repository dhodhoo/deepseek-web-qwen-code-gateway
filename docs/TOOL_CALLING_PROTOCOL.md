# Tool Calling Emulation Protocol

## Why this exists

Qwen Code expects the model/provider to decide when to call tools.

The current DeepSeek Web integration may not provide native OpenAI-style `tools[]` / `tool_calls`.

Therefore the gateway may emulate tool calling through a strict prompt protocol.

This is a compatibility mechanism, not native DeepSeek function calling.

## Responsibility boundary

```text
Model decides tool
Gateway translates decision
Qwen Code executes tool
```

The gateway must never execute Qwen Code's coding tools.

## Canonical internal tool

Normalize every incoming OpenAI function tool to:

```text
name
description
json_schema
```

Normalize a model tool decision to:

```text
id
name
arguments: dict
```

## Proposed control envelope

Use an uncommon explicit sentinel and one JSON object.

Recommended v1 protocol:

```text
<<<DSQG_TOOL_CALL>>>
{"name":"read","arguments":{"filePath":"src/main.py"}}
<<<DSQG_END_TOOL_CALL>>>
```

A final answer must not use this envelope.

The exact sentinel may change if testing finds a better one, but it must remain:

- explicit,
- easy to detect incrementally,
- unlikely to occur in normal source code/prose,
- documented,
- thoroughly tested.

## Prompt instruction concept

When tools exist, the gateway adds a system-level control instruction equivalent to:

```text
You may either answer normally or request exactly one tool.

Available tools:
<tool definitions and JSON schemas>

To request a tool, output only:

<<<DSQG_TOOL_CALL>>>
{"name":"TOOL_NAME","arguments":{...}}
<<<DSQG_END_TOOL_CALL>>>

Rules:
- Use only a listed tool.
- Arguments must match its JSON schema.
- Do not wrap the envelope in Markdown.
- Do not explain the call before or after the envelope.
- If no tool is needed, answer normally.
```

Do not copy this naively if prompt experiments find a more reliable formulation.

## Why one tool per model turn for v1

Qwen Code can repeatedly call the model.

Supporting one tool decision per model turn simplifies:

- parser correctness,
- streaming,
- validation,
- recovery.

Parallel tool calls may be added later after real acceptance testing proves a need.

## Tool-call parser requirements

A candidate becomes a tool call only if all conditions pass:

1. Start sentinel is present.
2. End sentinel is present.
3. Between them is exactly one parsable JSON object.
4. Object contains a string `name`.
5. Object contains object `arguments` (or a documented normalized fallback).
6. `name` exists in the tools supplied by the current client request.
7. Arguments are compatible with the supplied schema to a practical validation level.

Otherwise it is not a valid tool call.

## Unknown tool

If the model requests:

```text
delete_everything
```

but that tool was not supplied by Qwen Code, never emit it.

Preferred behavior:

- perform one bounded repair turn telling the model the call is invalid and listing valid tool names, or
- return a controlled provider error if repair is unsafe/unreliable.

Avoid infinite repair loops.

## Malformed JSON

Allowed bounded recovery may include:

- stripping surrounding whitespace,
- extracting exactly the content between sentinels,
- one deterministic JSON repair strategy for trivial trailing commas if explicitly tested.

Do not create a highly permissive parser that guesses arbitrary model intent.

If repair fails, use a bounded model repair turn or controlled error.

## Simulated tool use (no envelope attempted)

A model without native function calling may answer in PROSE that narrates
a tool loop — "I will list the files…" followed by fabricated listings or
file contents — instead of emitting an envelope. Such output must never
be treated as a tool call, and the instructions should forbid it
explicitly ("never simulate or narrate tool execution in prose").

Implemented behavior (ADR-029 → ADR-031 → ADR-035): EVERY tool-enabled
turn that ends without a valid envelope gets ONE bounded repair retry
before falling back to honest text — pre-loop plain text (ADR-029),
marker-bearing simulation (ADR-031), and marker-less mid-loop prose
alike (ADR-035, logged as `no_envelope`). The old termination guard —
marker-less mid-loop text presumed final, never repaired — was REMOVED
after live evidence falsified its premise three times: mid-loop turns
keep emitting marker-less intent prose ("I'll read the test file...")
instead of an envelope. Termination is still guaranteed BY CONSTRUCTION:
the budget is exactly one retry, and the non-simulation repair hint
explicitly permits a plain answer ("If no tool is actually needed,
answer normally in plain text"), so a genuine final answer answers
plainly again on attempt 2 and its text flushes. Accepted cost: every
envelope-less text answer pays one extra backend call.

HIGH-PRECISION simulation markers (ADR-031, extended by ADR-034) still
matter for the RETRY BASE: a text answer carrying a marker — the
control start sentinel as data, `[assistant tool call]`, `[tool result]`,
or fake conversation turns `[user]` / `[User]` / `[assistant]` — is
prose-simulated tool use and its retry is rebuilt on the stripped base
(see below). Detection scans ONLY the current inference output —
compiled history and tool RESULTS are input and never inspected
(shell/test output may contain anything).

Repair-retry base after a simulation trigger (ADR-033): the retry is
rebuilt on a STRIPPED compilation of the same history — every
tool-shaped message omitted, assistant text kept — because the live M8
failure showed the model copying whatever block format its context
displays (even annotated headers, ADR-032, superseded). The stripped
retry context presents no imitable template; the retry keeps the tool
instructions and a repair hint that names the envelope as the only way
to request a tool. Retries triggered for other reasons — including
marker-less `no_envelope` retries — keep the original prompt verbatim
(full context, informed calls). Canonical history is never stripped —
only the discarded retry-branch prompt is.

Main-path history rendering (ADR-034): historical assistant tool calls
compile BYTE-IDENTICAL to the instructed envelope itself
(`<<<DSQG_TOOL_CALL>>>` + the same JSON line + the end sentinel), so
the context's most tool-like pattern IS the valid request format —
imitation becomes a valid tool request instead of a failure mode. Tool
RESULTS keep their own `[tool result]` data blocks (results never use
the envelope format).

## Tool arguments

Public OpenAI response:

```json
{
  "function": {
    "name": "read",
    "arguments": "{\"filePath\":\"src/main.py\"}"
  }
}
```

Internally keep `arguments` as a dictionary after validation.

## Receiving tool results

Qwen Code will send a later message such as:

```json
{
  "role": "tool",
  "tool_call_id": "call_local_123",
  "content": "..."
}
```

The message compiler must convert this into unambiguous backend context.

Conceptual backend representation:

```text
TOOL RESULT
tool_call_id: call_local_123
tool: read
result:
<tool output>
END TOOL RESULT
```

Never reinterpret tool output as gateway control instructions.

Treat tool output as untrusted data.

## Prompt injection boundary

Tool output may contain text resembling:

```text
<<<DSQG_TOOL_CALL>>>
...
```

That must not be parsed as a new model tool call.

Only the assistant/model output for the current inference is eligible for control-envelope parsing.

## Streaming strategy

Implement a small state machine:

```text
NORMAL_TEXT
  ├─ ordinary data → stream text
  └─ possible sentinel prefix → CANDIDATE

CANDIDATE
  ├─ sentinel confirmed → BUFFER_TOOL
  └─ false alarm → flush buffered text and return NORMAL_TEXT

BUFFER_TOOL
  ├─ end sentinel found → validate
  │      ├─ valid → emit OpenAI tool call
  │      └─ invalid → repair/error policy
  └─ stream ends early → invalid/truncated policy
```

Do not partially stream the tool JSON as assistant text.

## Tool-call IDs

Generate gateway IDs, for example:

```text
call_dsqg_<random>
```

Store enough mapping so a later `role=tool` message can be associated with:

- the tool name,
- the original model turn,
- the conversation.

## Assistant history reconstruction

When the next request includes an assistant tool-call message, compile it back into backend context in a stable way.

Implemented (M6, ADR-023; envelope rendering ADR-034): historical assistant tool calls compile to the control-envelope block itself (byte-identical to the instructed format); `role=tool` results compile to `[tool result]` data blocks.

Do not rely only on remote DeepSeek memory.

## Acceptance sequence

The following must work:

```text
user asks to fix bug
→ model calls glob/read
→ Qwen Code returns result
→ model calls read
→ result
→ model calls edit/apply_patch
→ result
→ model calls bash
→ test result
→ final textual answer
```

At no point should the gateway itself read/write the user's codebase or run the project's shell commands.
