"""Deterministic OpenAI-messages → backend-prompt compiler.

Backends like DeepSeek Web accept a single prompt string, so the normalized
OpenAI message history must be compiled into one deterministic text. This is
the single place that does it (ARCHITECTURE.md: "The compiler must not rely
on lossy ad-hoc concatenation scattered across routes").

M2 scope was plain-text ``system`` / ``user`` / ``assistant`` messages.
M4 split compilation into two steps (:func:`messages_to_canonical` +
:func:`compile_canonical_to_prompt`) so the conversation manager can
compile per-turn deltas through the exact same code path (ADR-020).
M6 extends BOTH steps with the tool-shaped messages of
docs/TOOL_CALLING_PROTOCOL.md:

* assistant messages carrying ``tool_calls`` (content may be null) render
  as ``[assistant tool call]`` blocks;
* ``role=tool`` messages render as ``[tool result]`` blocks — untrusted
  DATA inside the prompt, never re-parsed as control envelopes (the
  protocol's injection boundary).

Anything still unrepresentable raises :class:`UnsupportedMessageError`
loudly: silently dropping tool history would break the tool_call_id
invariants of the master prompt.

The output format is stable and documented:

.. code-block:: text

    [system]
    <system text>

    [user]
    <user text>

    [assistant]
    <assistant text>

    [assistant tool call]
    id: <tool_call_id>
    tool: <function name>
    arguments: <compact arguments JSON>

    [tool result]
    id: <tool_call_id>
    tool: <function name>
    result:
    <tool output text>
    [end tool result]
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .conversation import CanonicalMessage, CanonicalToolCall
from .openai_types import ChatMessage
from .tools import normalize_arguments_json

__all__ = [
    "UnsupportedMessageError",
    "compile_canonical_to_prompt",
    "compile_messages_to_prompt",
    "extract_text",
    "messages_to_canonical",
]

_TEXT_ROLES = ("system", "user", "assistant")


class UnsupportedMessageError(ValueError):
    """A message shape this milestone cannot compile (clear, safe to expose
    in a 400 response; never contains secrets)."""


def extract_text(content: str | list[Any] | None) -> str:
    """Reduce message content to plain text.

    * ``None`` → ``""``
    * ``str`` → unchanged
    * ``list`` → the ``text`` of every ``{"type": "text", ...}`` part joined
      by newlines; non-text parts (image/audio/video/file) are ignored — the
      current Qwen Code client already substitutes text placeholders for
      unsupported media (source verification 2026-08-14).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(content)


def _tool_calls_to_canonical(
    raw_calls: Sequence[Any], where: str
) -> tuple[CanonicalToolCall, ...]:
    """Validate assistant ``tool_calls`` and normalize them (M6).

    Each entry must be an object with a non-empty string ``id`` and a
    ``function`` object holding a non-empty string ``name`` and arguments
    that normalize to a JSON object string (ADR-023: arguments are ALWAYS
    canonical compact JSON, on both directions of the wire).
    """
    calls: list[CanonicalToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        call_where = f"{where}.tool_calls[{index}]"
        if not isinstance(raw_call, dict):
            raise UnsupportedMessageError(
                f"{call_where}: each tool call must be an object"
            )
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise UnsupportedMessageError(
                f"{call_where}: missing non-empty tool call 'id'"
            )
        function = raw_call.get("function")
        if not isinstance(function, dict):
            raise UnsupportedMessageError(
                f"{call_where}: missing 'function' object"
            )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedMessageError(
                f"{call_where}: function 'name' must be a non-empty string"
            )
        try:
            arguments_json = normalize_arguments_json(
                function.get("arguments", {})
            )
        except ValueError as exc:
            raise UnsupportedMessageError(f"{call_where}: {exc}") from exc
        calls.append(
            CanonicalToolCall(
                id=call_id, function_name=name, arguments_json=arguments_json
            )
        )
    return tuple(calls)


def messages_to_canonical(
    messages: Sequence[ChatMessage],
) -> list[CanonicalMessage]:
    """Validate OpenAI messages and normalize them into canonical form (M4).

    This is the single validation gate for message shapes: anything this
    milestone cannot represent raises :class:`UnsupportedMessageError`.
    Since M6 this includes the tool shapes of
    docs/TOOL_CALLING_PROTOCOL.md: assistant ``tool_calls`` (with
    ``arguments`` normalized to compact JSON so the client's re-sent
    history round-trips through structural equality, ADR-023) and
    ``role=tool`` results (which REQUIRE their ``tool_call_id``). An
    assistant message with null content is valid only when it carries
    tool_calls — exactly the wire shape Qwen Code re-sends. The result is
    what the conversation store compares and keeps (ADR-020).
    """
    if not messages:
        raise UnsupportedMessageError("messages must not be empty")

    canonical: list[CanonicalMessage] = []
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        role = message.role

        if role == "tool":
            if message.tool_calls:
                raise UnsupportedMessageError(
                    f"{where}: tool_calls are only valid on assistant messages"
                )
            tool_call_id = message.tool_call_id
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise UnsupportedMessageError(
                    f"{where}: role 'tool' requires a non-empty tool_call_id"
                )
            name = (
                message.name
                if isinstance(message.name, str) and message.name
                else None
            )
            canonical.append(
                CanonicalMessage(
                    role="tool",
                    content=extract_text(message.content),
                    tool_call_id=tool_call_id,
                    name=name,
                )
            )
            continue

        if role not in _TEXT_ROLES:
            raise UnsupportedMessageError(
                f"{where}: unsupported role {role!r} "
                f"(expected one of {', '.join(_TEXT_ROLES)} or tool)"
            )
        if message.tool_calls and role != "assistant":
            raise UnsupportedMessageError(
                f"{where}: tool_calls are only valid on assistant messages"
            )

        tool_calls = None
        if role == "assistant" and message.tool_calls:
            tool_calls = _tool_calls_to_canonical(message.tool_calls, where)

        content: str | None = extract_text(message.content)
        if role == "assistant" and message.content is None:
            # Preserve the wire's null (tool-calls-only assistant message):
            # the client re-sends content=null, so canonical equality needs
            # None here, not "" (ADR-023 round-trip).
            content = None
        if role == "assistant" and content is None and tool_calls is None:
            raise UnsupportedMessageError(
                f"{where}: assistant message with null content requires "
                "tool_calls"
            )

        canonical.append(
            CanonicalMessage(role=role, content=content, tool_calls=tool_calls)
        )
    return canonical


def compile_canonical_to_prompt(
    messages: Sequence[CanonicalMessage],
    known_tool_names: Mapping[str, str] | None = None,
) -> str:
    """Compile canonical messages into one deterministic backend prompt.

    Works on any canonical sub-sequence (a full history or a per-turn
    delta, ADR-020). Since M6 this renders tool-shaped messages:
    assistant ``tool_calls`` as ``[assistant tool call]`` blocks and
    ``role=tool`` messages as ``[tool result]`` blocks. The tool name of a
    result resolves through the ids of the assistant tool calls seen
    EARLIER in the compiled sequence, then through ``known_tool_names``
    (the caller seeds this with the FULL request history when compiling a
    DELTA whose assistant tool call stays in stored state), then the
    result's own ``name`` field, then ``unknown`` — history is data,
    nothing more. Raises :class:`UnsupportedMessageError` for empty inputs
    and for shapes that remain unsupported (null-content assistant without
    tool_calls).
    """
    if not messages:
        raise UnsupportedMessageError("messages must not be empty")

    blocks: list[str] = []
    tool_name_by_id: dict[str, str] = dict(known_tool_names or {})
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        role = message.role

        if role == "assistant" and message.tool_calls:
            if message.content:
                blocks.append(f"[assistant]\n{message.content}")
            for call in message.tool_calls:
                tool_name_by_id[call.id] = call.function_name
                blocks.append(
                    "[assistant tool call]\n"
                    f"id: {call.id}\n"
                    f"tool: {call.function_name}\n"
                    f"arguments: {call.arguments_json}"
                )
            continue

        if role == "tool":
            tool_call_id = message.tool_call_id
            if not tool_call_id:
                raise UnsupportedMessageError(
                    f"{where}: role 'tool' requires a tool_call_id"
                )
            tool_name = (
                tool_name_by_id.get(tool_call_id) or message.name or "unknown"
            )
            blocks.append(
                "[tool result]\n"
                f"id: {tool_call_id}\n"
                f"tool: {tool_name}\n"
                "result:\n"
                f"{message.content or ''}\n"
                "[end tool result]"
            )
            continue

        if role in _TEXT_ROLES:
            if role == "assistant" and message.content is None:
                raise UnsupportedMessageError(
                    f"{where}: assistant message with null content requires "
                    "tool_calls"
                )
            blocks.append(f"[{role}]\n{message.content or ''}")
            continue

        raise UnsupportedMessageError(
            f"{where}: unsupported role {role!r} "
            f"(expected one of {', '.join(_TEXT_ROLES)} or tool)"
        )

    return "\n\n".join(blocks)


def compile_messages_to_prompt(messages: Sequence[ChatMessage]) -> str:
    """Compile normalized messages into one deterministic backend prompt.

    Raises :class:`UnsupportedMessageError` for empty histories and for
    shapes this milestone cannot compile. M2 entry point; the composition
    of :func:`messages_to_canonical` and
    :func:`compile_canonical_to_prompt` (M4).
    """
    return compile_canonical_to_prompt(messages_to_canonical(messages))
