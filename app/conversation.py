"""Canonical conversation state (M4).

The gateway's local canonical state is the SOURCE OF TRUTH for every
conversation; a backend remote session is an optimization/state link, never
the truth (docs/ARCHITECTURE.md). Everything here implements ADR-020:

* :class:`CanonicalMessage` / :class:`CanonicalToolCall` — the normalized,
  tool-history-capable message representation. Tool fields exist now and are
  populated from M6 on; plain chat fills ``role``/``content`` only.
* :class:`Conversation` — every field ARCHITECTURE.md requires
  (``conversation_id``, ``backend_type``, ``backend_account_id``,
  ``backend_session_id``, ``backend_parent_message_id``, ``created_at``,
  ``updated_at``, ``status``, normalized history) plus a plain-dict
  ``to_dict``/``from_dict`` round-trip — the "reconstructable
  representation" later failover/persistence builds on.
* :class:`ConversationStore` — bounded in-memory store (the v1 storage
  decision recorded in DECISIONS.md ADR-020): at most ``max_conversations``
  conversations, least-recently-updated evicted first, guarded by a lock.
  Nothing is ever written to disk — prompts/history live only in RAM
  ("never persist raw prompts/tool output by default").

Resolution semantics (ADR-020 point 3): a stored conversation matches an
incoming request when its history is a STRICT prefix of the incoming
canonical history; the longest match wins and the trailing delta (the new
messages) is returned. Equal or divergent histories do not match — the
caller then falls back to a new conversation compiled from the request's
own full history ("prefer correctness from the request's canonical message
history", docs/API_CONTRACT.md).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "CONVERSATION_STATUS_ACTIVE",
    "CanonicalToolCall",
    "CanonicalMessage",
    "Conversation",
    "ConversationStore",
    "ToolHistoryFindings",
    "tool_call_index",
    "validate_tool_history",
]

#: The only conversation status in M4. Later milestones may add states
#: (e.g. failed-over, archived); the field exists per ARCHITECTURE.md.
CONVERSATION_STATUS_ACTIVE = "active"

#: Default capacity of the in-memory store (ADR-020). Eviction is
#: self-healing: an evicted conversation's next request rebuilds from the
#: client's re-sent history.
DEFAULT_MAX_CONVERSATIONS = 256


# ---------------------------------------------------------------------------
# Canonical messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalToolCall:
    """One assistant tool call in canonical form (populated from M6).

    Mirrors the OpenAI shape: a stable ``id`` (the tool_call_id invariants
    of the master prompt), the function name, and ``arguments_json`` —
    arguments ALWAYS as a JSON string, matching the public wire contract.
    """

    id: str
    function_name: str
    arguments_json: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "function_name": self.function_name,
            "arguments_json": self.arguments_json,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanonicalToolCall:
        return cls(
            id=str(data["id"]),
            function_name=str(data["function_name"]),
            arguments_json=str(data["arguments_json"]),
        )


@dataclass(frozen=True)
class CanonicalMessage:
    """One normalized message of a canonical history.

    Plain chat (M4) uses ``role`` + ``content`` only. ``tool_calls`` /
    ``tool_call_id`` / ``name`` make the representation tool-history-capable
    for M6+ without a schema change. Equality is structural (frozen
    dataclass) — the conversation resolver relies on it.
    """

    role: str
    content: str | None = None
    tool_calls: tuple[CanonicalToolCall, ...] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            data["content"] = self.content
        if self.tool_calls is not None:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CanonicalMessage:
        tool_calls = data.get("tool_calls")
        return cls(
            role=str(data["role"]),
            content=data.get("content"),
            tool_calls=(
                tuple(CanonicalToolCall.from_dict(call) for call in tool_calls)
                if tool_calls is not None
                else None
            ),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
        )


# ---------------------------------------------------------------------------
# M7: persistent tool-call ID index + history validation (ADR-028)
# ---------------------------------------------------------------------------


def tool_call_index(
    messages: Sequence[CanonicalMessage],
) -> dict[str, CanonicalToolCall]:
    """Persistent tool-call ID mapping derived from canonical history (M7).

    Maps every assistant tool-call id in the history to its
    :class:`CanonicalToolCall`; the FIRST occurrence of an id wins
    (deterministic). Derived per request rather than stored: the index
    persists across turns because the client re-sends the full history,
    survives LRU eviction and restarts, and can never drift from the
    canonical state it indexes (ADR-028 point 4).
    """
    index: dict[str, CanonicalToolCall] = {}
    for message in messages:
        for call in message.tool_calls or ():
            index.setdefault(call.id, call)
    return index


@dataclass(frozen=True)
class ToolHistoryFindings:
    """Anomalies found by :func:`validate_tool_history` (M7, ADR-028).

    Findings are observability only — the lenient-in policy never rejects
    a request because of them (ADR-023).
    """

    #: tool_call_ids of role=tool messages matching no assistant tool call.
    orphan_tool_results: tuple[str, ...] = ()
    #: number of role=tool messages without a usable tool_call_id.
    missing_tool_call_ids: int = 0

    @property
    def clean(self) -> bool:
        return not self.orphan_tool_results and not self.missing_tool_call_ids


def validate_tool_history(
    messages: Sequence[CanonicalMessage],
) -> ToolHistoryFindings:
    """Check the tool pairing invariant over a canonical history (M7).

    The master prompt's invariant is ``assistant(tool_calls=[call_X]) →
    tool(tool_call_id=call_X) → next inference``. This reports — never
    rejects — violations of the PAIRING half: tool results whose id no
    assistant tool call ever issued, and tool results without an id.
    A tool call with no result YET is normal mid-loop and not reported.
    """
    index = tool_call_index(messages)
    orphans: list[str] = []
    missing = 0
    for message in messages:
        if message.role != "tool":
            continue
        if not message.tool_call_id:
            missing += 1
        elif message.tool_call_id not in index:
            orphans.append(message.tool_call_id)
    return ToolHistoryFindings(
        orphan_tool_results=tuple(orphans),
        missing_tool_call_ids=missing,
    )


# ---------------------------------------------------------------------------
# Conversation record
# ---------------------------------------------------------------------------


@dataclass
class Conversation:
    """One canonical conversation (ARCHITECTURE.md field list, ADR-020).

    ``backend_session_id`` / ``backend_parent_message_id`` are the live link
    into the backend's own session memory; both are ``None`` for a
    conversation whose link was never created or was invalidated after a
    failure (the next request then rebuilds from the canonical history).
    """

    conversation_id: str
    backend_type: str
    created_at: float
    updated_at: float
    messages: list[CanonicalMessage] = field(default_factory=list)
    backend_account_id: str | None = None  # multi-account arrives later
    backend_session_id: str | None = None
    backend_parent_message_id: str | None = None
    status: str = CONVERSATION_STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict (JSON-serializable) snapshot — the reconstructable
        representation. Contains message content by design: this is the
        in-RAM canonical state, never persisted to disk in v1 (ADR-020)."""
        return {
            "conversation_id": self.conversation_id,
            "backend_type": self.backend_type,
            "backend_account_id": self.backend_account_id,
            "backend_session_id": self.backend_session_id,
            "backend_parent_message_id": self.backend_parent_message_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "messages": [message.to_dict() for message in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Conversation:
        return cls(
            conversation_id=str(data["conversation_id"]),
            backend_type=str(data["backend_type"]),
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            messages=[
                CanonicalMessage.from_dict(message)
                for message in data.get("messages", [])
            ],
            backend_account_id=data.get("backend_account_id"),
            backend_session_id=data.get("backend_session_id"),
            backend_parent_message_id=data.get("backend_parent_message_id"),
            status=data.get("status", CONVERSATION_STATUS_ACTIVE),
        )


# ---------------------------------------------------------------------------
# In-memory bounded store
# ---------------------------------------------------------------------------


class ConversationStore:
    """Bounded in-memory canonical-state store (ADR-020).

    * Thread-safe: every public method takes an internal lock (Starlette
      runs sync handlers in a threadpool, so requests overlap on threads).
    * Bounded: at most ``max_conversations`` conversations; inserting beyond
      capacity evicts the least-recently-updated conversation.
    * Ephemeral: nothing is written to disk; a restart empties the store and
      the client's re-sent history reconstructs continuity (new backend
      session, full-history prompt — always correct, if less efficient).
    """

    def __init__(
        self,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
        clock=time.time,
    ) -> None:
        if max_conversations < 1:
            raise ValueError("max_conversations must be >= 1")
        self._max = max_conversations
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, Conversation] = {}

    # ------------------------------------------------------------------ read

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._by_id.get(conversation_id)

    def conversations(self) -> list[Conversation]:
        """All stored conversations, oldest-updated first (test/admin view)."""
        with self._lock:
            return sorted(
                self._by_id.values(), key=lambda c: (c.updated_at, c.created_at)
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)

    # ------------------------------------------------------------- lifecycle

    def put(self, conversation: Conversation) -> Conversation:
        """Insert (or replace) a conversation — used to rebuild a store from
        reconstructed snapshots (failover/restart path, ADR-020)."""
        with self._lock:
            self._by_id[conversation.conversation_id] = conversation
            self._evict_if_needed()
            return conversation

    def resolve(
        self,
        backend_type: str,
        incoming: Sequence[CanonicalMessage],
    ) -> tuple[Conversation | None, tuple[CanonicalMessage, ...]]:
        """Find the conversation this request continues (ADR-020 point 3).

        Returns ``(conversation, delta)`` where ``delta`` are the incoming
        messages beyond the stored history. ``(None, ())`` means "no match":
        the caller starts a new conversation from the full incoming history.
        A match requires the stored history to be a STRICT prefix of the
        incoming history (equal histories are duplicate re-sends, not
        continuations).
        """
        with self._lock:
            best: Conversation | None = None
            incoming_list = list(incoming)
            for conversation in self._by_id.values():
                if conversation.backend_type != backend_type:
                    continue
                stored = len(conversation.messages)
                if not 0 < stored < len(incoming_list):
                    continue
                if conversation.messages != incoming_list[:stored]:
                    continue
                if best is None or stored > len(best.messages):
                    best = conversation
            if best is None:
                return None, ()
            return best, tuple(incoming_list[len(best.messages) :])

    def commit_turn(
        self,
        backend_type: str,
        conversation: Conversation | None,
        incoming: Sequence[CanonicalMessage],
        assistant_message: CanonicalMessage,
        *,
        session_id: str,
        parent_message_id: str | None,
    ) -> Conversation:
        """Advance canonical state after a turn completed (ADR-020 point 5).

        History becomes exactly ``incoming + [assistant_message]`` — the
        request's own canonical history is the truth, healing any drift.
        The backend link (session + parent) records where upstream memory
        lives; ``parent_message_id`` is stored verbatim (``None`` when the
        backend exposed no id — it is never carried over from a previous
        turn, which would re-branch under the old parent).
        """
        with self._lock:
            now = self._clock()
            if conversation is None:
                conversation = Conversation(
                    conversation_id=f"conv_{uuid.uuid4().hex}",
                    backend_type=backend_type,
                    created_at=now,
                    updated_at=now,
                )
                self._by_id[conversation.conversation_id] = conversation
                self._evict_if_needed()
            conversation.messages = [*incoming, assistant_message]
            conversation.backend_session_id = session_id
            conversation.backend_parent_message_id = parent_message_id
            conversation.updated_at = now
            conversation.status = CONVERSATION_STATUS_ACTIVE
            return conversation

    def invalidate_backend_link(self, conversation: Conversation) -> None:
        """Drop the backend session/parent link after a failed turn.

        The canonical history is untouched; the next request rebuilds a
        fresh backend session from it (full-history prompt, ADR-020
        points 4–5).
        """
        with self._lock:
            conversation.backend_session_id = None
            conversation.backend_parent_message_id = None

    # --------------------------------------------------------------- internal

    def _evict_if_needed(self) -> None:
        """Evict least-recently-updated conversations beyond capacity."""
        while len(self._by_id) > self._max:
            victim_id = min(
                self._by_id,
                key=lambda cid: (
                    self._by_id[cid].updated_at,
                    self._by_id[cid].created_at,
                ),
            )
            del self._by_id[victim_id]
