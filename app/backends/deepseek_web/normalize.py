"""Normalization of raw DeepSeek Web stream data into internal events (M0).

Two entry points exist because the gateway consumes the upstream stream in
two forms:

1. :func:`parse_sse_line` + :func:`payload_to_events` — parse *raw* SSE
   lines (``data: {...}``) into full JSON payloads, then into events. This is
   the faithful path: it keeps fields (e.g. message ids) that the vendored
   client discards, and it is what fixtures recorded by the probe contain.

2. :func:`chunk_dict_to_events` — normalize the *reduced* chunk dicts
   ``{'content', 'type', 'finish_reason'}`` yielded by the vendored
   ``DeepSeekAPI.chat_completion`` generator.

Both paths produce the same stable event classes
(:mod:`app.backends.events`).

Also here: :func:`classify_upstream_exception`, the DeepSeek-specific mapping
from vendored exceptions into the generic error taxonomy
(:mod:`app.backends.errors`).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..errors import BackendErrorCategory, BackendFailure
from ..events import (
    BackendEvent,
    BackendMessageId,
    MessageFinished,
    ReasoningDelta,
    TextDelta,
    UnknownDelta,
)
from . import _vendor  # noqa: F401  (ensures vendored dsk is importable)

__all__ = [
    "RawStreamParseError",
    "parse_sse_line",
    "payload_to_events",
    "chunk_dict_to_events",
    "classify_upstream_exception",
    "SSE_DATA_PREFIX",
]

SSE_DATA_PREFIX = b"data: "


class RawStreamParseError(Exception):
    """A raw SSE data line carried malformed JSON."""


# ---------------------------------------------------------------------------
# Raw SSE parsing
# ---------------------------------------------------------------------------

def parse_sse_line(line: bytes | str) -> dict[str, Any] | None:
    """Parse one raw upstream SSE line.

    Returns the decoded JSON payload for ``data: {...}`` lines, or ``None``
    for everything else (empty keep-alives, ``event:``/``id:`` lines,
    comments, and non-object JSON payloads such as a hypothetical
    ``data: [DONE]`` — the vendored parser tolerates those by only looking
    for a ``choices`` key, and we must be at least as tolerant mid-stream).

    Raises :class:`RawStreamParseError` only when a ``data: `` line carries
    syntactically malformed JSON (the vendored client also fails hard there).
    """
    if isinstance(line, str):
        raw = line.encode("utf-8", errors="replace")
    else:
        raw = bytes(line)

    raw = raw.strip()
    if not raw.startswith(SSE_DATA_PREFIX):
        return None

    body = raw[len(SSE_DATA_PREFIX):]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RawStreamParseError(f"invalid JSON in SSE data line: {exc}") from exc

    if not isinstance(payload, dict):
        # Tolerated, mirroring the vendored parser (see docstring).
        return None
    return payload


# ---------------------------------------------------------------------------
# Payload/chunk -> events
# ---------------------------------------------------------------------------

#: Delta ``type`` values observed from DeepSeek Web (verified in M0 probe;
#: update docs/UPSTREAM_NOTES.md when new ones appear).
_KNOWN_DELTA_TYPES = {"text", "thinking"}


def _delta_events(content: Any, delta_type: Any) -> list[BackendEvent]:
    if not content:
        return []
    text = str(content)
    if delta_type == "thinking":
        return [ReasoningDelta(text)]
    if delta_type == "text":
        return [TextDelta(text)]
    if delta_type is None or delta_type == "":
        # Upstream sometimes omits the type for plain text; treat as text.
        return [TextDelta(text)]
    return [UnknownDelta(kind=str(delta_type), content=text)]


def payload_to_events(payload: dict[str, Any]) -> list[BackendEvent]:
    """Convert one full decoded SSE payload into internal events."""
    events: list[BackendEvent] = []

    payload_id = payload.get("id")
    if payload_id is not None and str(payload_id):
        events.append(BackendMessageId(id=str(payload_id)))

    choices = payload.get("choices")
    if not isinstance(choices, list):
        return events

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            events.extend(_delta_events(delta.get("content"), delta.get("type")))
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            events.append(MessageFinished(finish_reason=str(finish_reason)))
    return events


def chunk_dict_to_events(chunk: dict[str, Any]) -> list[BackendEvent]:
    """Convert one backend chunk dict into events.

    Consumes the chunk contract produced by the backend adapters: the legacy
    vendored shape ``{'content', 'type', 'finish_reason'}`` plus optional
    extensions from the current wire adapter (``response_message_id`` /
    ``request_message_id``, see :mod:`app.backends.deepseek_web.wire`).
    Missing keys are tolerated.
    """
    events: list[BackendEvent] = []
    response_message_id = chunk.get("response_message_id")
    if response_message_id:
        events.append(BackendMessageId(id=str(response_message_id)))
    events.extend(_delta_events(chunk.get("content"), chunk.get("type")))
    finish_reason = chunk.get("finish_reason")
    if finish_reason is not None:
        events.append(MessageFinished(finish_reason=str(finish_reason)))
    return events


def normalize_stream_chunks(
    chunks: Iterable[dict[str, Any]],
) -> Iterable[BackendEvent]:
    """Convenience generator over a full vendored-client chunk stream."""
    for chunk in chunks:
        yield from chunk_dict_to_events(chunk)


# ---------------------------------------------------------------------------
# Upstream exception -> taxonomy mapping
# ---------------------------------------------------------------------------

def classify_upstream_exception(exc: BaseException) -> BackendFailure:
    """Map a vendored deepseek4free exception to a normalized failure.

    Never include credential material in the message; vendored messages are
    static strings (no token echo), but we still rebuild messages defensively
    where they could carry upstream response bodies.
    """
    from dsk import api as dsk_api  # vendored; import here to keep lazy

    status = getattr(exc, "status_code", None)

    if isinstance(exc, dsk_api.AuthenticationError):
        return BackendFailure(
            BackendErrorCategory.AUTH_INVALID,
            "DeepSeek Web authentication failed (invalid or expired token)",
            cause=exc,
        )
    if isinstance(exc, dsk_api.RateLimitError):
        return BackendFailure(
            BackendErrorCategory.RATE_LIMITED,
            "DeepSeek Web rate limit exceeded",
            status_code=429,
            cause=exc,
        )
    if isinstance(exc, dsk_api.CloudflareError):
        return BackendFailure(
            BackendErrorCategory.CLOUDFLARE_BLOCKED,
            "DeepSeek Web request blocked by Cloudflare",
            cause=exc,
        )
    if isinstance(exc, dsk_api.NetworkError):
        return BackendFailure(
            BackendErrorCategory.UPSTREAM_NETWORK,
            "Network failure while talking to DeepSeek Web",
            cause=exc,
        )
    if isinstance(exc, dsk_api.APIError):
        message = str(exc)
        if status is not None and status >= 500:
            return BackendFailure(
                BackendErrorCategory.UPSTREAM_5XX,
                "DeepSeek Web server error",
                status_code=status,
                cause=exc,
            )
        if "cloudflare" in message.lower():
            return BackendFailure(
                BackendErrorCategory.CLOUDFLARE_BLOCKED,
                "Cloudflare protection could not be satisfied",
                cause=exc,
            )
        if status is not None:
            return BackendFailure(
                BackendErrorCategory.UPSTREAM_PROTOCOL,
                f"Unexpected DeepSeek Web response status {status}",
                status_code=status,
                cause=exc,
            )
        return BackendFailure(
            BackendErrorCategory.UPSTREAM_PROTOCOL,
            "DeepSeek Web stream/response protocol error",
            cause=exc,
        )
    if isinstance(exc, RawStreamParseError):
        return BackendFailure(
            BackendErrorCategory.UPSTREAM_PROTOCOL,
            "Malformed SSE data from DeepSeek Web",
            cause=exc,
        )
    if isinstance(exc, ValueError):
        return BackendFailure(
            BackendErrorCategory.CLIENT_BAD_REQUEST,
            str(exc),
            cause=exc,
        )
    return BackendFailure(
        BackendErrorCategory.INTERNAL,
        f"Unexpected backend error: {type(exc).__name__}",
        cause=exc,
    )
