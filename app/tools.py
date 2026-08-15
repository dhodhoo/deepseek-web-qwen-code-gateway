"""Incoming-tool normalization and tool prompt instructions (M6).

DeepSeek Web accepts a single prompt string and has no native OpenAI-style
function calling, so the gateway emulates tool calling through a strict
prompt protocol (ADR-006, docs/TOOL_CALLING_PROTOCOL.md):

* :func:`normalize_tools` reduces the client's OpenAI ``tools[]`` to
  :class:`CanonicalTool` records (name / description / JSON schema);
* :func:`build_tool_instructions` renders the deterministic control
  instruction block the server appends to the compiled prompt when tools
  are enabled;
* :func:`normalize_arguments_json` / :func:`arguments_compatible` are the
  validation helpers shared by the envelope parser (model output) and the
  message compiler (client history).

Policy decisions are recorded in DECISIONS.md ADR-023. In particular the
normalizer is LENIENT on the way in (malformed tool entries are skipped,
never rejected — Qwen Code sends well-formed tools, and rejecting a whole
request over one odd entry would break chat), while the envelope parser is
STRICT on the way out (only a fully valid envelope ever becomes a
structured ``tool_calls`` response — never fabricate).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "TOOL_CALL_START_SENTINEL",
    "TOOL_CALL_END_SENTINEL",
    "CanonicalTool",
    "normalize_tools",
    "normalize_arguments_json",
    "arguments_compatible",
    "build_tool_instructions",
]

#: Control-envelope sentinels (docs/TOOL_CALLING_PROTOCOL.md "Proposed
#: control envelope"). Explicit, incrementally detectable, and unlikely to
#: occur in normal source code or prose.
TOOL_CALL_START_SENTINEL = "<<<DSQG_TOOL_CALL>>>"
TOOL_CALL_END_SENTINEL = "<<<DSQG_END_TOOL_CALL>>>"

#: Tool names the gateway accepts. Matches what Qwen Code produces for its
#: built-in tools and normalized MCP tool names (letters, digits, ``_``,
#: ``-``; e.g. ``read_file``, ``computer_use__click``).
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class CanonicalTool:
    """One normalized client-supplied tool (TOOL_CALLING_PROTOCOL.md
    "Canonical internal tool")."""

    name: str
    description: str
    schema: dict[str, Any] | None


def normalize_tools(raw_tools: Sequence[Any] | None) -> list[CanonicalTool]:
    """Normalize OpenAI ``tools[]`` into canonical tools.

    Only ``{"type": "function", "function": {...}}`` entries with a valid
    name participate; anything else is skipped silently (ADR-023: lenient
    request handling — the alternative, rejecting the whole request, would
    break plain chat over one odd entry). Duplicate names: first wins.
    Returns ``[]`` for absent/empty input or when nothing valid remains.
    """
    normalized: list[CanonicalTool] = []
    seen: set[str] = set()
    for raw in raw_tools or []:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_PATTERN.match(name):
            continue
        if name in seen:
            continue
        description = function.get("description")
        parameters = function.get("parameters")
        normalized.append(
            CanonicalTool(
                name=name,
                description=description if isinstance(description, str) else "",
                schema=parameters if isinstance(parameters, dict) else None,
            )
        )
        seen.add(name)
    return normalized


def normalize_arguments_json(arguments: Any) -> str:
    """Normalize tool-call arguments to the canonical compact JSON string.

    Accepts a dict or a JSON string parsing to a dict (the OpenAI wire
    carries a JSON STRING in both directions). The output is deterministic
    (compact separators, UTF-8 preserved) so the gateway's emitted
    arguments and the client's re-sent arguments compare equal in the
    canonical conversation state (ADR-023). Raises ``ValueError`` when the
    arguments are not a JSON object.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError as exc:
            raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc
    else:
        parsed = arguments
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def arguments_compatible(
    arguments: dict[str, Any], schema: dict[str, Any] | None
) -> bool:
    """Pragmatic schema compatibility check (envelope rule 7).

    Deliberately PRACTICAL, not full JSON-Schema validation
    (TOOL_CALLING_PROTOCOL.md): ``required`` properties must be present and
    every provided property with a declared scalar/container ``type`` must
    match it shallowly. Unknown properties are allowed; undeclared or
    unrecognized types pass. Strictness beyond this buys nothing against a
    backend that interprets arguments as prose, and a false rejection here
    loses a genuine tool call.
    """
    if not isinstance(arguments, dict):
        return False
    if not isinstance(schema, dict):
        return True

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in arguments:
                return False

    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    for key, value in arguments.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue
        declared = prop.get("type")
        if isinstance(declared, str) and not _value_matches_type(value, declared):
            return False
    return True


def _value_matches_type(value: Any, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "null":
        return value is None
    return True  # unknown/union type declarations: lenient


def build_tool_instructions(
    tools: Sequence[CanonicalTool], *, required: bool = False
) -> str:
    """Render the deterministic tool-control instruction block (M6).

    Appended AFTER the compiled message blocks by the server (never by the
    message compiler itself) so it appears exactly once per request whether
    the turn compiles a full history or only a delta (ADR-020 session
    reuse). ``required`` reflects ``tool_choice: 'required'`` — Qwen
    Code's only other value, ``'none'``, disables tools entirely upstream
    of this function (ADR-023).

    Rendering is COMPACTED for the upstream prompt budget (ADR-024): a
    real Qwen Code agent turn can carry ~70 tools whose full descriptions
    and schema prose inflate the block to 100 KB+ — a prompt DeepSeek Web
    stalls on (live evidence). Each description is reduced to its first
    non-empty line capped at a fixed length, and ``description`` keys are
    stripped from the rendered schema (types/required/enum are kept).
    Validation is unaffected: the envelope parser still checks arguments
    against the FULL un-compacted schema.

    Wording is ANTI-SIMULATION (ADR-029): a live M7 acceptance turn
    showed the model answering in prose by NARRATING a tool loop with
    fabricated results instead of emitting an envelope. The optional
    branch and the envelope rules therefore forbid simulated or narrated
    tool execution explicitly. ADR-031 extends this: the M8 acceptance
    showed the model imitating the gateway's OWN internal history blocks
    (``[assistant tool call]`` / ``[tool result]``), so the wording also
    marks those blocks as history-only data that must never be output.
    """
    lines: list[str] = ["[available tools]"]
    if required:
        lines.append(
            "You MUST request exactly one tool call now by outputting the "
            "control envelope below. Do not answer with plain text."
        )
    else:
        lines.append(
            "Answer the latest user message. You may either answer normally "
            "or request exactly one tool call using the control envelope "
            "below. If you need a tool to answer, you MUST request it with "
            "the control envelope — you cannot execute tools yourself; "
            "NEVER simulate or narrate tool execution in prose (no "
            "fabricated listings or results, and never '[assistant tool "
            "call]' or '[tool result]' blocks)."
        )
    lines.append("")
    lines.append("Available tools:")
    lines.append("")
    for tool in tools:
        description = _compact_description(tool.description)
        if description:
            lines.append(f"- {tool.name}: {description}")
        else:
            lines.append(f"- {tool.name}")
        if tool.schema is not None:
            schema_json = json.dumps(
                _strip_schema_descriptions(tool.schema),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            lines.append(f"  parameters: {schema_json}")
    lines.append("")
    lines.append(
        "To request a tool call, output EXACTLY this envelope and nothing "
        "else:"
    )
    lines.append("")
    lines.append(TOOL_CALL_START_SENTINEL)
    lines.append('{"name":"<tool name>","arguments":{"<parameter>":"<value>"}}')
    lines.append(TOOL_CALL_END_SENTINEL)
    lines.append("")
    lines.append("Envelope rules:")
    lines.append('- "name" must be one of the tools listed above.')
    lines.append(
        '- "arguments" must be a JSON object matching that tool\'s '
        "parameters schema."
    )
    lines.append(
        "- Output at most ONE envelope and no text around it: no markdown "
        "fences, no explanation before or after."
    )
    lines.append(
        "- If no tool is needed, answer normally without any envelope."
    )
    lines.append(
        "- Never describe a tool call in prose; either output a real "
        "envelope or answer normally."
    )
    lines.append(
        "- '[assistant tool call]' and '[tool result]' blocks in the "
        "conversation are HISTORY data only; never output those markers — "
        "the envelope above is the ONLY way to request a tool."
    )
    return "\n".join(lines)


#: First-line description cap for the rendered instruction block
#: (ADR-024). Long enough to keep the tool-selection signal for Qwen
#: Code's verbose tool descriptions, short enough to keep a 70-tool block
#: within the upstream prompt budget.
_DESCRIPTION_MAX_CHARS = 150


def _compact_description(description: str) -> str:
    """First non-empty line of a tool description, length-capped."""
    for line in description.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > _DESCRIPTION_MAX_CHARS:
            return stripped[: _DESCRIPTION_MAX_CHARS - 1].rstrip() + "\u2026"
        return stripped
    return ""


def _strip_schema_descriptions(schema: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a JSON schema without its ``description`` members.

    Keeps every validation-relevant member (type/properties/required/
    items/enum/default/...) — the rendered block only loses prose
    (ADR-024). Non-dict/list values pass through unchanged.
    """

    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _clean(item)
                for key, item in value.items()
                if key != "description"
            }
        if isinstance(value, list):
            return [_clean(item) for item in value]
        return value

    return _clean(schema)
