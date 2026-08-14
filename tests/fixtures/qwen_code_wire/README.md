# Qwen Code wire fixtures (M5)

Request bodies covering the wire format Qwen Code sends to an
OpenAI-compatible provider, fixtured per milestone M5 ("the exact current
agent request/history format is documented and covered by tests").

## Provenance

These bodies were **synthesized from source verification**, then
**corrected against real traffic**: Qwen Code v0.21.11 (commit
`a669957f`, verified 2026-08-14). On 2026-08-14 a real Qwen Code
v0.21.11 (win32; x64) installation was connected to the gateway with
`GATEWAY_DIAGNOSTICS_DIR` enabled (M5 live acceptance); the structural
diff of the captured requests confirmed every source-verified fact and
folded three corrections back here (`max_tokens` value, `temperature`
presence, the `respond_in_schema` side-query shape — see
`docs/UPSTREAM_NOTES.md`, "Live traffic verification"). Raw captures stay
in the user's private diagnostics directory because they contain real
prompts; fixtures carry synthesized content only.

Future drift checks: with `GATEWAY_DIAGNOSTICS_DIR` set, the gateway
appends the actual requests to `<dir>/requests.jsonl`
(`app/diagnostics.py`). Compare new captures against these fixtures and
update the fixtures if the real client drifts.

## Files

| File                                | Shape                                                                                                                                                                                              | Gateway behavior (M5)                                                                              |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `agent_turn_stream_with_tools.json` | Agent turn: `stream: true` + `stream_options.include_usage`, non-empty `tools[]`, no `tool_choice` (plain turn)                                                                                    | 200; tools accepted and ignored (plain-text answer; structured `tool_calls` arrive in M6, ADR-021) |
| `plain_chat_non_stream.json`        | Side query: explicit `stream: false`, no tools                                                                                                                                                     | 200 non-stream JSON                                                                                |
| `side_query_respond_in_schema.json` | Structured side query (**traffic-verified**): explicit `stream: false`, `tool_choice: 'required'`, single `respond_in_schema` tool                                                                 | 200 non-stream plain text; tool/tool_choice accepted and ignored (ADR-021)                         |
| `tool_history_turn.json`            | Tool-loop continuation: assistant `tool_calls` (`content: null`, arguments as JSON string) + `role=tool` with content as an ARRAY of text parts                                                    | 400 `UNSUPPORTED_MESSAGE` until M6 — pinned deterministically by tests                             |
| `non_standard_extras.json`          | Non-standard fields the client may send (`reasoning_effort`, `enable_thinking`, `thinking`, `chat_template_kwargs`, `preserve_thinking`, `metadata`, `cache_control`, `vl_high_resolution_images`) | 200; lenient parsing, extras ignored                                                               |

Verified wire facts encoded here:

- `stream` is always explicit (never defaulted); agent turns add
  `stream_options: {"include_usage": true}` — the gateway emits no usage
  chunk and the client tolerates its absence;
- `tools[]` entries are `{type: "function", function: {name, description,
parameters}}` — no `strict`, no `parallel_tool_calls` (traffic: uniform
  across up to 69 tools in one request, including MCP tools);
- `tool_choice` is only ever `'required'` or `'none'` (never `'auto'`);
- assistant tool-call history uses `content: null` plus `tool_calls[]` with
  `arguments` as a JSON STRING; `role=tool` carries `tool_call_id` and by
  default content as an array of `{type: "text", text}` parts;
- `max_tokens` is always present and may be large (traffic: `32000` on the
  captured install);
- `temperature` is sent when configured (traffic: `0` on every captured
  request).
