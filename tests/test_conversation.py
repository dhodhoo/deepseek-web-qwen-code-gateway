"""M4 unit tests: canonical state types + ConversationStore (offline).

Covers ADR-020's state model: tool-history-capable canonical messages, the
ARCHITECTURE.md conversation fields with their reconstructable dict
round-trip, history-prefix resolution, commit-on-finish store semantics,
bounds/eviction, and thread safety.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.conversation import (
    CONVERSATION_STATUS_ACTIVE,
    CanonicalMessage,
    CanonicalToolCall,
    Conversation,
    ConversationStore,
)


def _msg(role: str, content: str | None) -> CanonicalMessage:
    return CanonicalMessage(role=role, content=content)


# ---------------------------------------------------------------------------
# Canonical message types
# ---------------------------------------------------------------------------


class TestCanonicalMessages:
    def test_plain_message_dict_round_trip(self) -> None:
        message = _msg("user", "hello")
        assert CanonicalMessage.from_dict(message.to_dict()) == message

    def test_tool_shaped_messages_round_trip(self) -> None:
        # The representation is tool-history-capable NOW (populated from M6):
        # assistant-with-tool_calls and role=tool results must survive the
        # dict round-trip without loss, tool_call_id included.
        call = CanonicalToolCall(
            id="call_1", function_name="read", arguments_json='{"filePath":"x"}'
        )
        assistant = CanonicalMessage(
            role="assistant", content=None, tool_calls=(call,)
        )
        tool = CanonicalMessage(
            role="tool", content="file-body", tool_call_id="call_1", name="read"
        )
        for message in (assistant, tool):
            snapshot = json.loads(json.dumps(message.to_dict()))
            assert CanonicalMessage.from_dict(snapshot) == message

    def test_to_dict_omits_absent_optional_fields(self) -> None:
        assert _msg("user", "hi").to_dict() == {"role": "user", "content": "hi"}

    def test_equality_is_structural(self) -> None:
        assert _msg("user", "a") == _msg("user", "a")
        assert _msg("user", "a") != _msg("user", "b")
        assert _msg("user", "a") != _msg("assistant", "a")


# ---------------------------------------------------------------------------
# Conversation record
# ---------------------------------------------------------------------------


def _conversation() -> Conversation:
    return Conversation(
        conversation_id="conv_abc",
        backend_type="deepseek_web",
        created_at=1000.0,
        updated_at=2000.0,
        messages=[_msg("user", "one"), _msg("assistant", "two")],
        backend_account_id=None,
        backend_session_id="sess-1",
        backend_parent_message_id="resp-1",
    )


class TestConversationRecord:
    def test_dict_round_trip_preserves_every_architecture_field(self) -> None:
        clone = Conversation.from_dict(_conversation().to_dict())
        assert clone.conversation_id == "conv_abc"
        assert clone.backend_type == "deepseek_web"
        assert clone.backend_account_id is None  # present, unset until M10
        assert clone.backend_session_id == "sess-1"
        assert clone.backend_parent_message_id == "resp-1"
        assert clone.created_at == 1000.0
        assert clone.updated_at == 2000.0
        assert clone.status == CONVERSATION_STATUS_ACTIVE
        assert clone.messages == _conversation().messages

    def test_snapshot_is_json_serializable(self) -> None:
        snapshot = json.dumps(_conversation().to_dict())
        clone = Conversation.from_dict(json.loads(snapshot))
        assert clone.to_dict() == _conversation().to_dict()


# ---------------------------------------------------------------------------
# Store: resolution
# ---------------------------------------------------------------------------


class TestStoreResolve:
    def _store(self) -> ConversationStore:
        return ConversationStore()

    def test_empty_store_never_matches(self) -> None:
        conversation, delta = self._store().resolve(
            "deepseek_web", [_msg("user", "hi")]
        )
        assert conversation is None
        assert delta == ()

    def test_strict_prefix_match_returns_the_delta(self) -> None:
        store = self._store()
        created = store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        incoming = [
            _msg("user", "one"),
            _msg("assistant", "a1"),
            _msg("user", "two"),
        ]
        conversation, delta = store.resolve("deepseek_web", incoming)
        assert conversation is created
        assert delta == (_msg("user", "two"),)

    def test_equal_history_is_a_duplicate_not_a_match(self) -> None:
        store = self._store()
        store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id=None,
        )
        conversation, delta = store.resolve(
            "deepseek_web", [_msg("user", "one"), _msg("assistant", "a1")]
        )
        assert conversation is None
        assert delta == ()

    def test_divergent_history_does_not_match(self) -> None:
        store = self._store()
        store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id=None,
        )
        conversation, delta = store.resolve(
            "deepseek_web",
            [_msg("user", "one"), _msg("assistant", "OTHER"), _msg("user", "two")],
        )
        assert conversation is None
        assert delta == ()

    def test_longest_prefix_wins(self) -> None:
        store = self._store()
        shorter = store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id=None,
        )
        longer = store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one"), _msg("assistant", "a1"), _msg("user", "two")],
            _msg("assistant", "a2"),
            session_id="s1",
            parent_message_id=None,
        )
        incoming = [
            _msg("user", "one"),
            _msg("assistant", "a1"),
            _msg("user", "two"),
            _msg("assistant", "a2"),
            _msg("user", "three"),
        ]
        conversation, delta = store.resolve("deepseek_web", incoming)
        assert conversation is longer
        assert conversation is not shorter
        assert delta == (_msg("user", "three"),)

    def test_other_backend_types_do_not_match(self) -> None:
        store = self._store()
        store.commit_turn(
            "deepseek_web",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id=None,
        )
        conversation, delta = store.resolve(
            "fake", [_msg("user", "one"), _msg("assistant", "a1"), _msg("user", "two")]
        )
        assert conversation is None
        assert delta == ()


# ---------------------------------------------------------------------------
# Store: commit / invalidate
# ---------------------------------------------------------------------------


class TestStoreCommit:
    def test_commit_creates_a_conversation_with_incoming_plus_reply(self) -> None:
        store = ConversationStore()
        conversation = store.commit_turn(
            "fake",
            None,
            [_msg("system", "be brief"), _msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        assert conversation.conversation_id.startswith("conv_")
        assert conversation.backend_type == "fake"
        assert conversation.status == CONVERSATION_STATUS_ACTIVE
        assert conversation.backend_session_id == "s1"
        assert conversation.backend_parent_message_id == "resp-1"
        assert conversation.messages == [
            _msg("system", "be brief"),
            _msg("user", "one"),
            _msg("assistant", "a1"),
        ]
        assert store.get(conversation.conversation_id) is conversation
        assert len(store) == 1

    def test_commit_replaces_history_with_the_requests_truth(self) -> None:
        # History becomes exactly incoming + reply — the request's canonical
        # history heals any drift (ADR-020 point 5).
        store = ConversationStore()
        conversation = store.commit_turn(
            "fake",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        store.commit_turn(
            "fake",
            conversation,
            [_msg("user", "one"), _msg("assistant", "a1"), _msg("user", "two")],
            _msg("assistant", "a2"),
            session_id="s1",
            parent_message_id="resp-2",
        )
        assert conversation.messages == [
            _msg("user", "one"),
            _msg("assistant", "a1"),
            _msg("user", "two"),
            _msg("assistant", "a2"),
        ]
        assert conversation.backend_parent_message_id == "resp-2"
        assert len(store) == 1  # updated in place, no duplicate row

    def test_parent_id_is_stored_verbatim_and_never_carried_over(self) -> None:
        # A turn whose backend exposed no message id stores None — carrying
        # the previous parent over would re-branch under the old response.
        store = ConversationStore()
        conversation = store.commit_turn(
            "fake",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        store.commit_turn(
            "fake",
            conversation,
            [_msg("user", "one"), _msg("assistant", "a1"), _msg("user", "two")],
            _msg("assistant", "a2"),
            session_id="s1",
            parent_message_id=None,
        )
        assert conversation.backend_parent_message_id is None

    def test_commit_advances_updated_at(self) -> None:
        ticks = iter([100.0, 200.0])
        store = ConversationStore(clock=lambda: next(ticks))
        conversation = store.commit_turn(
            "fake",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id=None,
        )
        assert conversation.created_at == 100.0
        assert conversation.updated_at == 100.0
        store.commit_turn(
            "fake",
            conversation,
            [_msg("user", "one"), _msg("assistant", "a1"), _msg("user", "two")],
            _msg("assistant", "a2"),
            session_id="s1",
            parent_message_id=None,
        )
        assert conversation.created_at == 100.0
        assert conversation.updated_at == 200.0

    def test_invalidate_clears_only_the_backend_link(self) -> None:
        store = ConversationStore()
        conversation = store.commit_turn(
            "fake",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        store.invalidate_backend_link(conversation)
        assert conversation.backend_session_id is None
        assert conversation.backend_parent_message_id is None
        assert conversation.messages == [
            _msg("user", "one"),
            _msg("assistant", "a1"),
        ]


# ---------------------------------------------------------------------------
# Store: bounds + reconstruction + concurrency
# ---------------------------------------------------------------------------


class TestStoreBoundsAndReconstruction:
    def test_capacity_evicts_least_recently_updated(self) -> None:
        ticks = iter([1.0, 2.0, 3.0])
        store = ConversationStore(max_conversations=2, clock=lambda: next(ticks))
        first = store.commit_turn(
            "fake", None, [_msg("user", "1")], _msg("assistant", "a"),
            session_id="s1", parent_message_id=None,
        )
        second = store.commit_turn(
            "fake", None, [_msg("user", "2")], _msg("assistant", "a"),
            session_id="s2", parent_message_id=None,
        )
        third = store.commit_turn(
            "fake", None, [_msg("user", "3")], _msg("assistant", "a"),
            session_id="s3", parent_message_id=None,
        )
        assert len(store) == 2
        assert store.get(first.conversation_id) is None  # evicted (oldest)
        assert store.get(second.conversation_id) is second
        assert store.get(third.conversation_id) is third

    def test_max_conversations_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ConversationStore(max_conversations=0)

    def test_put_rebuilds_a_store_from_a_reconstructed_snapshot(self) -> None:
        # The restart/failover path (ADR-020): snapshot → dict → new store.
        source = ConversationStore()
        conversation = source.commit_turn(
            "fake",
            None,
            [_msg("user", "one")],
            _msg("assistant", "a1"),
            session_id="s1",
            parent_message_id="resp-1",
        )
        rebuilt = ConversationStore()
        rebuilt.put(Conversation.from_dict(json.loads(json.dumps(conversation.to_dict()))))
        clone = rebuilt.get(conversation.conversation_id)
        assert clone is not None
        assert clone.to_dict() == conversation.to_dict()

    def test_conversations_lists_oldest_updated_first(self) -> None:
        ticks = iter([1.0, 2.0])
        store = ConversationStore(clock=lambda: next(ticks))
        first = store.commit_turn(
            "fake", None, [_msg("user", "1")], _msg("assistant", "a"),
            session_id="s1", parent_message_id=None,
        )
        second = store.commit_turn(
            "fake", None, [_msg("user", "2")], _msg("assistant", "a"),
            session_id="s2", parent_message_id=None,
        )
        assert store.conversations() == [first, second]

    def test_concurrent_commits_do_not_corrupt_the_store(self) -> None:
        store = ConversationStore(max_conversations=1000)

        def worker(worker_id: int) -> None:
            for i in range(25):
                store.commit_turn(
                    "fake",
                    None,
                    [_msg("user", f"w{worker_id}-{i}")],
                    _msg("assistant", "r"),
                    session_id="s",
                    parent_message_id=None,
                )

        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(store) == 200
        assert all(len(c.messages) == 2 for c in store.conversations())
