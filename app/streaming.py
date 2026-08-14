"""Normalized backend events → OpenAI SSE chunks (M3).

This module is the ONLY translator between :mod:`app.backends.events` and
the public streaming wire format (docs/API_CONTRACT.md "Streaming
response"). Guarantees:

* **No raw leakage.** Only ``TextDelta`` text and the mapped finish reason
  ever reach the wire. ``ReasoningDelta`` (vendor-internal thinking),
  ``BackendMessageId``, ``UnknownDelta`` and every other event render to
  NOTHING — upstream framing, ids and control data never leave the gateway.
* **Deterministic shape.** Every chunk is a standard OpenAI
  ``chat.completion.chunk``: same ``id``/``created``/``model`` across one
  stream, ``choices[0].delta`` carries ``role`` on the first rendered chunk
  and ``content`` increments afterwards, one terminal chunk carries
  ``finish_reason`` with an empty delta, then ``data: [DONE]`` terminates.
* **Honest errors.** Failures BEFORE the first byte are surfaced by the
  route as regular HTTP statuses (priming, see ``app/server.py`` — the Qwen
  Code client keys retry behavior off HTTP status). Failures MID-stream
  (after HTTP 200 headers are committed) are emitted as an OpenAI-style
  ``data: {"error": {...}}`` envelope and the stream closes WITHOUT
  ``[DONE]`` — the openai SDK raises on the error event, and a missing
  ``[DONE]`` unambiguously marks an incomplete stream.
* **No usage chunk.** DeepSeek Web exposes no token counts; the verified
  Qwen Code client tolerates a missing usage chunk even when it sends
  ``stream_options.include_usage`` (docs/UPSTREAM_NOTES.md).

Threading: backend iterators are synchronous/blocking; they are consumed via
:meth:`starlette.concurrency.iterate_in_threadpool` so the event loop is
never blocked. On client disconnect Starlette stops consuming and closes
this async generator; the in-flight upstream turn simply runs to completion
in the threadpool (stateless M2/M3 session policy — nothing to roll back).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterator

from starlette.concurrency import iterate_in_threadpool

from .backends.errors import BackendErrorCategory, BackendFailure
from .backends.events import (
    BackendError,
    BackendEvent,
    MessageFinished,
    MessageStarted,
    TextDelta,
)
from .error_mapping import backend_failure_to_response

__all__ = [
    "STREAM_EMPTY",
    "SSE_DONE",
    "backend_event_to_chunk",
    "sse_data_line",
    "sse_stream",
]

#: Terminal SSE line (OpenAI convention; never emitted after an error).
SSE_DONE = "data: [DONE]\n\n"


class _StreamEmpty:
    """Sentinel: the backend produced zero events for this turn."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "STREAM_EMPTY"


STREAM_EMPTY = _StreamEmpty()


def backend_event_to_chunk(
    event: BackendEvent,
) -> tuple[dict[str, Any], str | None] | None:
    """Map one normalized event to ``(delta, finish_reason)``.

    Returns ``None`` when the event must not produce any public chunk
    (reasoning / ids / unknown deltas — never leaked).
    """
    if isinstance(event, MessageStarted):
        return {"role": "assistant", "content": ""}, None
    if isinstance(event, TextDelta):
        return {"content": event.text}, None
    if isinstance(event, MessageFinished):
        return {}, _map_finish_reason(event.finish_reason)
    return None


def _map_finish_reason(reason: str | None) -> str:
    """Backend finish reason → OpenAI finish_reason (M2/M3 rule)."""
    if reason == "length":
        return "length"
    return "stop"


def _category_or_internal(kind: str) -> BackendErrorCategory:
    try:
        return BackendErrorCategory(kind)
    except ValueError:
        return BackendErrorCategory.INTERNAL


def sse_data_line(payload: dict[str, Any]) -> str:
    """Render one SSE ``data:`` line (compact JSON, UTF-8, CRLF-free)."""
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _render_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }


async def sse_stream(
    primed: BackendEvent | _StreamEmpty,
    events: Iterator[BackendEvent],
    *,
    chunk_id: str,
    created: int,
    model: str,
) -> AsyncIterator[str]:
    """Yield the public SSE lines for one turn.

    ``primed`` is the first event already pulled by the route (so pre-stream
    failures could answer with an HTTP status), or :data:`STREAM_EMPTY` when
    the backend produced nothing. ``events`` is the remaining iterator.
    """
    meta = {"chunk_id": chunk_id, "created": created, "model": model}
    role_sent = False

    def render(event: BackendEvent) -> list[str]:
        nonlocal role_sent
        if isinstance(event, BackendError):
            # Defensive: current backends raise BackendFailure (ADR-011/014);
            # normalize the event surface into the same failure path.
            raise BackendFailure(
                category=_category_or_internal(event.kind),
                message=event.message,
                retryable=event.retryable,
                status_code=event.status_code,
            )
        parts = backend_event_to_chunk(event)
        if parts is None:
            return []
        delta, finish_reason = parts
        if not role_sent:
            delta = {"role": "assistant", **delta}
            role_sent = True
        return [
            sse_data_line(
                _render_chunk(**meta, delta=delta, finish_reason=finish_reason)
            )
        ]

    try:
        if primed is STREAM_EMPTY:
            # Degenerate turn: emit a clean role + terminal pair anyway so
            # clients always observe a well-formed empty completion.
            for line in render(MessageStarted()):
                yield line
            for line in render(MessageFinished("stop")):
                yield line
        else:
            if isinstance(primed, BaseException):
                # Same defensive rule as the loop below, for the primed item.
                raise primed
            for line in render(primed):
                yield line
            async for event in iterate_in_threadpool(events):
                if isinstance(event, BaseException):
                    # Defensive: backends must RAISE failures, never yield
                    # them; treat a yielded exception as that failure.
                    raise event
                for line in render(event):
                    yield line
        yield SSE_DONE
    except BackendFailure as failure:
        # Headers are already committed (HTTP 200): surface the failure as
        # an in-stream error envelope and close WITHOUT [DONE] (ADR-019).
        _, body = backend_failure_to_response(failure)
        yield sse_data_line({"error": body["error"]})
