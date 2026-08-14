"""FakeBackend — deterministic in-memory LLMBackend for tests and dev.

Implements the stable :class:`app.backends.base.LLMBackend` interface with
fully scripted behavior:

* each ``stream_turn`` consumes the NEXT scripted turn (a sequence of
  normalized events and/or exception instances to raise);
* every call is recorded for assertions;
* nothing is random, nothing touches the network.

Intended uses:

* unit tests for every layer above the backend boundary (M2+), with no
  dependency on the vendored DeepSeek client;
* local gateway development without a DeepSeek credential
  (``GATEWAY_BACKEND=fake``; see ``app/config.py``).

Deliberately strict: when the script is exhausted, ``stream_turn`` raises a
``BackendFailure(INTERNAL)`` instead of inventing output — silent default
behavior would hide test bugs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from .base import BackendHealth, BackendSession, LLMBackend
from .errors import BackendErrorCategory, BackendFailure
from .events import BackendEvent, MessageFinished, MessageStarted, TextDelta

__all__ = ["FakeBackend", "TurnCall", "fake_text_turn"]


@dataclass(frozen=True)
class TurnCall:
    """Recorded arguments of one ``stream_turn`` invocation."""

    session_id: str
    prompt: str
    parent_message_id: str | None
    thinking_enabled: bool
    search_enabled: bool


def fake_text_turn(
    text: str = "OK",
    *,
    finish_reason: str = "stop",
) -> list[BackendEvent]:
    """Script helper: one coherent plain-text turn (started/text/finished)."""
    return [MessageStarted(), TextDelta(text), MessageFinished(finish_reason)]


class FakeBackend(LLMBackend):
    """Scripted, dependency-free backend implementing :class:`LLMBackend`."""

    backend_type = "fake"

    def __init__(
        self,
        turns: Sequence[Sequence[BackendEvent | BaseException]] | None = None,
    ) -> None:
        """``turns``: one scripted turn per upcoming ``stream_turn`` call.

        Each turn is a sequence of normalized events to yield in order;
        a ``BaseException`` instance in the sequence is raised at that point
        (use :class:`BackendFailure` to exercise the error taxonomy).
        """
        self._turns: list[list[BackendEvent | BaseException]] = [
            list(turn) for turn in (turns or [])
        ]
        #: Every stream_turn call, in order (for assertions).
        self.turn_calls: list[TurnCall] = []
        #: Every session created, in order.
        self.sessions_created: list[BackendSession] = []

    # ------------------------------------------------------------------ info

    def health_check(self) -> BackendHealth:
        return BackendHealth(backend_type=self.backend_type, ready=True)

    # -------------------------------------------------------------- session

    def create_session(self) -> BackendSession:
        session = BackendSession(
            session_id=f"fake-session-{len(self.sessions_created) + 1}"
        )
        self.sessions_created.append(session)
        return session

    # --------------------------------------------------------------- stream

    def stream_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        parent_message_id: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
    ) -> Iterator[BackendEvent]:
        self.turn_calls.append(
            TurnCall(
                session_id=session_id,
                prompt=prompt,
                parent_message_id=parent_message_id,
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
            )
        )
        if not self._turns:
            raise BackendFailure(
                category=BackendErrorCategory.INTERNAL,
                message="FakeBackend script exhausted: no scripted turn left",
            )
        script = self._turns.pop(0)
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item
