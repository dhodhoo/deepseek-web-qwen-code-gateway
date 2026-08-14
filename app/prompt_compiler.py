"""Deterministic OpenAI-messages → backend-prompt compiler (M2 subset).

Backends like DeepSeek Web accept a single prompt string, so the normalized
OpenAI message history must be compiled into one deterministic text. This is
the single place that does it (ARCHITECTURE.md: "The compiler must not rely
on lossy ad-hoc concatenation scattered across routes").

M2 scope: plain-text ``system`` / ``user`` / ``assistant`` messages only.
Tool-shaped messages (``role=tool``, assistant ``tool_calls``, null-content
tool-only assistant messages) raise :class:`UnsupportedMessageError` with a
clear message — they are compiled by the tool-aware compiler of M6+
(docs/TOOL_CALLING_PROTOCOL.md). Rejecting loudly is deliberate: silently
dropping tool history would break the tool_call_id invariants later.

The output format is stable and documented (M6 will extend it with the
tool-result representation from TOOL_CALLING_PROTOCOL.md, not change it):

.. code-block:: text

    [system]
    <system text>

    [user]
    <user text>

    [assistant]
    <assistant text>
"""

from __future__ import annotations

from typing import Any, Sequence

from .openai_types import ChatMessage

__all__ = ["UnsupportedMessageError", "compile_messages_to_prompt", "extract_text"]

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


def compile_messages_to_prompt(messages: Sequence[ChatMessage]) -> str:
    """Compile normalized messages into one deterministic backend prompt.

    Raises :class:`UnsupportedMessageError` for empty histories and for any
    tool-related shape (M6+ scope).
    """
    if not messages:
        raise UnsupportedMessageError("messages must not be empty")

    blocks: list[str] = []
    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        role = message.role

        if role == "tool":
            raise UnsupportedMessageError(
                f"{where}: role 'tool' is not supported yet "
                "(tool calling arrives in milestone M6)"
            )
        if role not in _TEXT_ROLES:
            raise UnsupportedMessageError(
                f"{where}: unsupported role {role!r} "
                f"(expected one of {', '.join(_TEXT_ROLES)})"
            )
        if message.tool_calls:
            raise UnsupportedMessageError(
                f"{where}: assistant tool_calls are not supported yet "
                "(tool calling arrives in milestone M6)"
            )
        if role == "assistant" and message.content is None:
            # Null-content assistant messages are tool-calls-only in current
            # Qwen Code; there is nothing to compile in M2.
            raise UnsupportedMessageError(
                f"{where}: assistant message with null content is not "
                "supported yet (tool calling arrives in milestone M6)"
            )

        blocks.append(f"[{role}]\n{extract_text(message.content)}")

    return "\n\n".join(blocks)
