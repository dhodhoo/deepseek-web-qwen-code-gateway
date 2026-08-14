"""Stable backend interface (M1).

Everything above the backend layer (API routes, message compiler, tool
emulation, conversation manager) programs against :class:`LLMBackend` and the
normalized types in this module only. Concrete backends (DeepSeek Web today;
a fake for tests; possibly others later) implement the interface.

Design notes (see docs/DECISIONS.md ADR-014):

* The interface is an ABC, not a ``typing.Protocol``: all backends live in
  this repository, nominal subtyping makes conformance explicit, and missing
  methods fail fast at instantiation instead of at first use.
* ``create_session`` returns a :class:`BackendSession` and ``health_check``
  a :class:`BackendHealth` (both named by docs/ARCHITECTURE.md) instead of a
  bare ``str``/``dict`` so later milestones can extend them without breaking
  the signature.
* ``stream_turn`` implementations may accept additional keyword options
  (e.g. ``DeepSeekWebBackend`` accepts ``raw_sink`` for probe capture), but
  nothing above the backend layer may rely on options outside this
  interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

from .events import BackendEvent

__all__ = [
    "LLMBackend",
    "BackendSession",
    "BackendHealth",
]


@dataclass(frozen=True)
class BackendSession:
    """A backend chat session created by :meth:`LLMBackend.create_session`.

    ``session_id`` is opaque to the rest of the application: it is passed
    back to ``stream_turn`` verbatim and stored as canonical state (M4),
    never parsed or interpreted.
    """

    session_id: str


@dataclass(frozen=True)
class BackendHealth:
    """Local (no-network) health snapshot of a backend.

    ``details`` carries backend-specific, NON-sensitive facts (e.g. whether
    cookies were loaded). Secrets and credential material must never appear
    here; ``DeepSeekWebBackend.health_check`` is tested against that rule.
    """

    backend_type: str
    ready: bool
    details: dict[str, Any] = field(default_factory=dict)


class LLMBackend(ABC):
    """The stable contract between the gateway and any model backend.

    Implementations must:

    * set a class-level ``backend_type`` identifier (stable, lowercase,
      usable in settings and canonical state);
    * raise :class:`app.backends.errors.BackendFailure` (normalized taxonomy)
      for any backend/upstream problem, whether at session creation or
      mid-stream — see docs/DECISIONS.md ADR-011 for the exception-vs-event
      rule;
    * yield only normalized :class:`BackendEvent` objects from
      ``stream_turn`` — raw upstream framing never crosses this boundary.
    """

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Stable identifier for this backend implementation.

        Satisfied by a plain class attribute (e.g.
        ``backend_type = "deepseek_web"``).
        """

    @abstractmethod
    def health_check(self) -> BackendHealth:
        """Return local health information. Must not perform network I/O
        and must never include secrets."""

    @abstractmethod
    def create_session(self) -> BackendSession:
        """Create a new backend chat session.

        Raises :class:`app.backends.errors.BackendFailure` on failure.
        """

    @abstractmethod
    def stream_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        parent_message_id: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
    ) -> Iterator[BackendEvent]:
        """Run one prompt turn inside ``session_id`` and yield normalized
        events.

        ``prompt`` is already-compiled backend input (the OpenAI
        messages-to-prompt compilation happens above this layer, M6+).
        ``parent_message_id`` threads turns when the backend supports it
        (M4); backends that ignore it must document that.

        Failures are raised as
        :class:`app.backends.errors.BackendFailure`, potentially after some
        events were already yielded (mid-stream failure).
        """
