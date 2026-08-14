"""Normalized internal backend events (M0).

These are the *stable internal types* required by ``docs/ARCHITECTURE.md``:
upstream DeepSeek stream data must be converted into these events before any
API/tool logic ever sees it. The OpenAI SSE schema is deliberately NOT used
here so backends stay replaceable.

Event inventory (suggested by ARCHITECTURE.md, implemented 1:1):

* :class:`TextDelta`       — a chunk of visible assistant text
* :class:`ReasoningDelta`  — internal/thinking content (vendor-specific data)
* :class:`MessageStarted`  — model started producing a message
* :class:`MessageFinished` — terminal chunk carrying the finish reason
* :class:`BackendMessageId` — backend-assigned message/session identifier
* :class:`BackendError`    — normalized failure surfaced as an event

M0 extension (documented, tested):

* :class:`UnknownDelta` — a delta with an unrecognized ``type``. Upstream
  types can change at any time; dropping unknown data silently would hide
  upstream drift, so it is preserved as an explicit event that callers may
  ignore/log. See docs/DECISIONS.md (ADR-010).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

__all__ = [
    "BackendEvent",
    "TextDelta",
    "ReasoningDelta",
    "MessageStarted",
    "MessageFinished",
    "BackendMessageId",
    "BackendError",
    "UnknownDelta",
]


@dataclass(frozen=True)
class TextDelta:
    """A chunk of visible assistant text."""

    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    """DeepSeek 'thinking' content. Internal/vendor data, never tool output."""

    text: str


@dataclass(frozen=True)
class MessageStarted:
    """The backend signaled the start of an assistant message.

    ``backend_message_id`` is populated when the upstream payload exposes an
    identifier at message start (observed behavior is verified during M0 live
    probing; may remain ``None`` if upstream never signals one).
    """

    backend_message_id: str | None = None


@dataclass(frozen=True)
class MessageFinished:
    """Terminal event for one model turn.

    ``finish_reason`` carries the upstream value verbatim (e.g. ``"stop"``).
    Normalization of upstream reasons to OpenAI reasons is a later layer's
    job, not the event model's.
    """

    finish_reason: str | None = None


@dataclass(frozen=True)
class BackendMessageId:
    """A backend-assigned identifier for the produced message.

    Needed later for ``parent_message_id`` threading and canonical state
    (M4). Upstream may expose it inside the streamed payload; the vendored
    client's reduced chunks drop it, which is why the gateway parses raw SSE
    payloads itself.
    """

    id: str


@dataclass(frozen=True)
class UnknownDelta:
    """A streamed delta whose ``type`` is not recognized.

    Preserved (rather than dropped) so upstream drift is observable.
    """

    kind: str
    content: str = ""


@dataclass(frozen=True)
class BackendError:
    """A normalized backend failure expressed as an event.

    ``kind`` uses the categories from
    :class:`app.backends.errors.BackendErrorCategory`.
    """

    kind: str
    retryable: bool
    message: str
    status_code: int | None = None
    details: dict = field(default_factory=dict)


BackendEvent = Union[
    TextDelta,
    ReasoningDelta,
    MessageStarted,
    MessageFinished,
    BackendMessageId,
    BackendError,
    UnknownDelta,
]
