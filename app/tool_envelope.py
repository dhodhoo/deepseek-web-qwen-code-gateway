"""Strict control-envelope parser for prompt-emulated tool calling (M6).

Implements the state machine of docs/TOOL_CALLING_PROTOCOL.md ("Streaming
strategy") over the sentinels defined in :mod:`app.tools`:

.. code-block:: text

    NORMAL_TEXT
      ├─ ordinary data → emit text
      └─ possible start-sentinel prefix → hold back (CANDIDATE)
    IN_ENVELOPE
      ├─ end sentinel found → validate
      │     ├─ valid → emit ONE ToolCallEmitted, discard later text
      │     └─ invalid → flush the raw region as text (honest), keep
      │        scanning, set ``invalid_envelope_seen`` (M7: the server's
      │        bounded repair policy may retry the turn — ADR-028)
      └─ stream ends early → flush the raw region as text (truncation
         also sets ``invalid_envelope_seen``)

Guarantees:

* Envelope content is NEVER partially streamed as assistant text — the
  region between sentinels is held back until the end sentinel decides.
* Only a FULLY valid envelope becomes a tool call (protocol rules 1–7):
  both sentinels, exactly one parsable JSON object, string ``name`` known
  in the current request's tools, JSON-object ``arguments`` (missing →
  normalized ``{}`` fallback), pragmatic schema compatibility.
* The gateway never fabricates tool calls: anything not fully valid is
  rendered as the plain text the model actually produced.
* Simulation markers (ADR-031): :data:`SIMULATION_MARKERS` names the
  strings whose presence in a turn's flushed OUTPUT marks prose-simulated
  tool use (imitation of the control protocol or of the prompt compiler's
  internal history blocks). The server checks them against the assembled
  buffered-turn text to decide its bounded repair retry.
* Injection boundary: ONLY the current inference output is ever parsed.
  Tool RESULTS live in the compiled prompt (input), never in this parser,
  and a later model turn re-parses from scratch with the tools of ITS OWN
  request — untrusted history cannot pre-register tool names.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from .tools import (
    TOOL_CALL_END_SENTINEL,
    TOOL_CALL_START_SENTINEL,
    CanonicalTool,
    arguments_compatible,
    normalize_arguments_json,
)

__all__ = [
    "EmittedToolCall",
    "ToolCallEmitted",
    "EnvelopeParser",
    "SIMULATION_MARKERS",
    "new_tool_call_id",
    "parse_envelope_from_text",
]

#: Simulation markers (ADR-031, extended by ADR-034): strings that belong
#: ONLY to the control protocol, to the prompt compiler's history blocks,
#: or to serialized chat transcripts. Their presence in a tool-enabled
#: turn's flushed OUTPUT means the model imitated the gateway's own
#: format instead of emitting an envelope — prose-simulated tool use.
#: The live M8 failures wrote simulated loops as ``[assistant tool
#: call]`` / ``[tool result]`` blocks (the pre-ADR-034 history format)
#: and, later, fake ``[User]`` / ``[user]`` / ``[assistant]`` turn
#: transcripts (capture record 91, 2026-08-15). All markers are
#: high-precision: a genuine final answer never contains them. The
#: server checks these against the ASSEMBLED buffered-turn text
#: (chunk-split-proof); scope is the current inference output only —
#: compiled history and tool results are INPUT and never inspected
#: (injection boundary).
SIMULATION_MARKERS = (
    TOOL_CALL_START_SENTINEL,
    "[assistant tool call]",
    "[tool result]",
    "[user]",
    "[User]",
    "[assistant]",
)


@dataclass(frozen=True)
class EmittedToolCall:
    """One validated gateway tool call, ready for wire rendering.

    ``arguments_json`` is the canonical compact JSON string (the exact
    value that goes into the OpenAI ``function.arguments`` field).
    """

    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolCallEmitted:
    """Gateway-internal stream item consumed by ``app/streaming.py`` and the
    canonical-state recorder: exactly one validated tool call."""

    call: EmittedToolCall


def new_tool_call_id() -> str:
    """Generate a gateway tool-call id (``call_dsqg_<32 hex>``).

    The id round-trips verbatim through Qwen Code (assistant ``tool_calls``
    → later ``role=tool.tool_call_id``), satisfying the tool_call_id
    invariants of the master prompt.
    """
    return f"call_dsqg_{uuid.uuid4().hex}"


def _candidate_prefix_len(text: str, sentinel: str) -> int:
    """Longest trailing slice of ``text`` that could still grow into
    ``sentinel`` (strictly shorter than the sentinel itself)."""
    max_k = min(len(text), len(sentinel) - 1)
    for k in range(max_k, 0, -1):
        if text.endswith(sentinel[:k]):
            return k
    return 0


class EnvelopeParser:
    """Incremental strict parser over one model turn's text output.

    Usage: :meth:`feed` every ``TextDelta`` in order, then :meth:`finalize`
    at end of stream. Outputs are ``str`` (text to render) and at most one
    :class:`ToolCallEmitted` (the FIRST valid envelope wins; all text after
    a valid envelope is discarded per the protocol's "no text around the
    envelope" rule).

    :attr:`invalid_envelope_seen` (M7, ADR-028) records whether the model
    clearly TRIED to use the control format but failed it — an invalid
    region between sentinels, or a truncated envelope at end of stream.
    The server's bounded repair policy reads it to decide on one repair
    retry. Plain held-back text never sets it, nor does a valid emission.
    """

    def __init__(self, tools: Sequence[CanonicalTool]) -> None:
        self._tools_by_name = {tool.name: tool for tool in tools}
        self._pending = ""
        self._in_envelope = False
        #: The validated tool call, once one is emitted.
        self.emitted_call: EmittedToolCall | None = None
        self._done = False
        self._invalid_envelope_seen = False

    @property
    def invalid_envelope_seen(self) -> bool:
        """True once a malformed/truncated envelope was flushed (M7).

        Read-only; set inside :meth:`feed` (invalid region between both
        sentinels) and :meth:`finalize` (stream ended inside an
        envelope). Consumed by the server's bounded repair policy
        (ADR-028 point 2); one fresh parser per attempt keeps the flag
        scoped to its own inference.
        """
        return self._invalid_envelope_seen

    # ------------------------------------------------------------------ feed

    def feed(self, text: str) -> list[Any]:
        """Consume a text increment; return renderable outputs (if any)."""
        outputs: list[Any] = []
        if self._done or not text:
            return outputs
        self._pending += text
        while not self._done and self._pending:
            if self._in_envelope:
                end = self._pending.find(TOOL_CALL_END_SENTINEL)
                if end == -1:
                    break  # keep accumulating envelope content
                content = self._pending[:end]
                self._pending = self._pending[
                    end + len(TOOL_CALL_END_SENTINEL) :
                ]
                self._in_envelope = False
                call = self._validate(content)
                if call is not None:
                    self.emitted_call = call
                    self._done = True
                    self._pending = ""
                    outputs.append(ToolCallEmitted(call=call))
                    break
                # Invalid region: flush it raw (honest — never silently
                # drop model output) and keep scanning the remainder. The
                # flag tells the server the model attempted the format
                # (M7 bounded repair trigger, ADR-028).
                self._invalid_envelope_seen = True
                outputs.append(
                    TOOL_CALL_START_SENTINEL + content + TOOL_CALL_END_SENTINEL
                )
            else:
                start = self._pending.find(TOOL_CALL_START_SENTINEL)
                if start == -1:
                    hold = _candidate_prefix_len(
                        self._pending, TOOL_CALL_START_SENTINEL
                    )
                    emit_len = len(self._pending) - hold
                    if emit_len > 0:
                        outputs.append(self._pending[:emit_len])
                        self._pending = self._pending[emit_len:]
                    break
                if start > 0:
                    outputs.append(self._pending[:start])
                self._pending = self._pending[
                    start + len(TOOL_CALL_START_SENTINEL) :
                ]
                self._in_envelope = True
        return outputs

    def finalize(self) -> list[Any]:
        """End of stream: flush whatever is still held back.

        A truncated envelope (no end sentinel) is flushed as raw text —
        the honest fallback — and flags ``invalid_envelope_seen`` so the
        server's bounded repair policy (ADR-028) can retry the turn.
        """
        if self._done:
            return []
        outputs: list[Any] = []
        if self._in_envelope:
            # Stream ended INSIDE an envelope: truncated tool attempt.
            self._invalid_envelope_seen = True
            outputs.append(TOOL_CALL_START_SENTINEL + self._pending)
        elif self._pending:
            outputs.append(self._pending)
        self._pending = ""
        self._in_envelope = False
        return outputs

    # -------------------------------------------------------------- internal

    def _validate(self, content: str) -> EmittedToolCall | None:
        """Apply protocol rules 3–7 to the region between the sentinels."""
        try:
            parsed = json.loads(content.strip())
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        name = parsed.get("name")
        if not isinstance(name, str) or name not in self._tools_by_name:
            return None
        try:
            arguments_json = normalize_arguments_json(
                parsed.get("arguments", {})
            )
        except ValueError:
            return None
        tool = self._tools_by_name[name]
        if not arguments_compatible(json.loads(arguments_json), tool.schema):
            return None
        return EmittedToolCall(
            id=new_tool_call_id(), name=name, arguments_json=arguments_json
        )


def parse_envelope_from_text(
    text: str, tools: Sequence[CanonicalTool]
) -> tuple[list[str], EmittedToolCall | None]:
    """Parse one COMPLETE model output (non-streaming convenience).

    Returns ``(visible_text_parts, tool_call_or_none)`` — the same
    semantics as the incremental parser in one shot.
    """
    parser = EnvelopeParser(tools)
    outputs = parser.feed(text) + parser.finalize()
    text_parts = [out for out in outputs if isinstance(out, str)]
    return text_parts, parser.emitted_call
