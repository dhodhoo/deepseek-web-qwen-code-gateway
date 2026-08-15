"""M4 tests: multi-turn conversation continuity through the public API.

``FakeBackend`` records every ``stream_turn`` call (session id, prompt,
parent id), so these offline tests prove the full ADR-020 behavior:

* history-prefix conversation resolution from the request's own messages;
* backend session reuse + delta-only prompts for matched conversations;
* ``parent_message_id`` threading from captured ``BackendMessageId`` events;
* commit-on-finish (partial turns never touch canonical history);
* invalidation + full-history rebuild after a ``BackendFailure``;
* reconstruction: a store rebuilt from snapshots continues the SAME
  backend session ("multi-turn plain chat is correct and locally
  reconstructable" — the M4 exit criterion).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import (
    BackendMessageId,
    MessageFinished,
    MessageStarted,
    TextDelta,
)
from app.backends.fake import FakeBackend
from app.config import GatewaySettings
from app.conversation import CanonicalMessage, Conversation, ConversationStore
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(
    backend: FakeBackend,
    store: ConversationStore | None = None,
    settings: GatewaySettings | None = None,
) -> TestClient:
    return TestClient(create_app(settings or _settings(), backend, store))


def _turn(text: str, message_id: str | None = None, *, finish: str = "stop") -> list:
    events: list = [MessageStarted()]
    if message_id is not None:
        events.append(BackendMessageId(message_id))
    events.append(TextDelta(text))
    events.append(MessageFinished(finish))
    return events


def _chat(messages: list, **overrides) -> dict:
    body = {"model": MODEL, "messages": messages, "stream": False}
    body.update(overrides)
    return body


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _single_conversation(store: ConversationStore) -> Conversation:
    conversations = store.conversations()
    assert len(conversations) == 1
    return conversations[0]


def _stream_all(client: TestClient, payload: dict) -> list[str]:
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=AUTH
    ) as response:
        assert response.status_code == 200
        return [line for line in response.iter_lines() if line.strip()]


# ---------------------------------------------------------------------------
# Non-streaming multi-turn
# ---------------------------------------------------------------------------


class TestMultiTurnNonStreaming:
    def test_three_turns_reuse_one_session_and_thread_parents(self) -> None:
        backend = FakeBackend(
            turns=[_turn("A1", "resp-1"), _turn("A2", "resp-2"), _turn("A3", "resp-3")]
        )
        store = ConversationStore()
        client = _client(backend, store)

        response = client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A1"

        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.json()["choices"][0]["message"]["content"] == "A2"

        response = client.post(
            "/v1/chat/completions",
            json=_chat(
                [
                    _user("one"),
                    _assistant("A1"),
                    _user("two"),
                    _assistant("A2"),
                    _user("three"),
                ]
            ),
            headers=AUTH,
        )
        assert response.json()["choices"][0]["message"]["content"] == "A3"

        # ONE backend session for the whole conversation.
        assert [s.session_id for s in backend.sessions_created] == ["fake-session-1"]
        calls = backend.turn_calls
        assert [call.session_id for call in calls] == ["fake-session-1"] * 3

        # Parent threading: turn N+1 parents under turn N's backend id.
        assert calls[0].parent_message_id is None
        assert calls[1].parent_message_id == "resp-1"
        assert calls[2].parent_message_id == "resp-2"

        # Delta prompts: only the NEW message goes to the backend — the
        # upstream session already holds prior context.
        assert calls[0].prompt == "[user]\none"
        assert calls[1].prompt == "[user]\ntwo"
        assert calls[2].prompt == "[user]\nthree"

        # Canonical state is the full local truth.
        conversation = _single_conversation(store)
        assert conversation.backend_type == "fake"
        assert conversation.backend_session_id == "fake-session-1"
        assert conversation.backend_parent_message_id == "resp-3"
        assert conversation.messages == [
            CanonicalMessage(role="user", content="one"),
            CanonicalMessage(role="assistant", content="A1"),
            CanonicalMessage(role="user", content="two"),
            CanonicalMessage(role="assistant", content="A2"),
            CanonicalMessage(role="user", content="three"),
            CanonicalMessage(role="assistant", content="A3"),
        ]

    def test_missing_backend_message_id_leaves_parent_unset(self) -> None:
        backend = FakeBackend(turns=[_turn("A1"), _turn("A2")])  # no ids
        store = ConversationStore()
        client = _client(backend, store)
        client.post("/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH)
        client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        # Session reuse still applies; the parent is never invented.
        assert backend.turn_calls[1].session_id == "fake-session-1"
        assert backend.turn_calls[1].parent_message_id is None
        assert _single_conversation(store).backend_parent_message_id is None

    def test_divergent_history_starts_a_new_conversation(self) -> None:
        backend = FakeBackend(turns=[_turn("A1", "resp-1"), _turn("B1")])
        store = ConversationStore()
        client = _client(backend, store)
        client.post("/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH)
        client.post(
            "/v1/chat/completions", json=_chat([_user("different")]), headers=AUTH
        )
        assert [s.session_id for s in backend.sessions_created] == [
            "fake-session-1",
            "fake-session-2",
        ]
        assert len(store) == 2
        assert backend.turn_calls[1].parent_message_id is None
        assert backend.turn_calls[1].prompt == "[user]\ndifferent"

    def test_resending_a_completed_turn_is_a_new_conversation(self) -> None:
        # Equal histories are duplicate re-sends, not continuations
        # (ADR-020 point 3): the request falls back to a fresh conversation
        # compiled from its own full history.
        backend = FakeBackend(turns=[_turn("A1", "resp-1"), _turn("A1-again")])
        store = ConversationStore()
        client = _client(backend, store)
        client.post("/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH)
        client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1")]),
            headers=AUTH,
        )
        assert len(backend.sessions_created) == 2
        assert len(store) == 2
        assert backend.turn_calls[1].prompt == "[user]\none\n\n[assistant]\nA1"


# ---------------------------------------------------------------------------
# Streaming multi-turn
# ---------------------------------------------------------------------------


class TestMultiTurnStreaming:
    def test_two_streamed_turns_share_session_and_thread_parents(self) -> None:
        backend = FakeBackend(turns=[_turn("A1", "resp-1"), _turn("A2", "resp-2")])
        store = ConversationStore()
        client = _client(backend, store)

        lines = _stream_all(client, _chat([_user("one")], stream=True))
        assert lines[-1] == "data: [DONE]"

        lines = _stream_all(
            client,
            _chat([_user("one"), _assistant("A1"), _user("two")], stream=True),
        )
        assert lines[-1] == "data: [DONE]"

        assert [s.session_id for s in backend.sessions_created] == ["fake-session-1"]
        assert backend.turn_calls[1].parent_message_id == "resp-1"
        assert backend.turn_calls[1].prompt == "[user]\ntwo"

        conversation = _single_conversation(store)
        assert conversation.backend_session_id == "fake-session-1"
        assert conversation.backend_parent_message_id == "resp-2"
        assert conversation.messages == [
            CanonicalMessage(role="user", content="one"),
            CanonicalMessage(role="assistant", content="A1"),
            CanonicalMessage(role="user", content="two"),
            CanonicalMessage(role="assistant", content="A2"),
        ]


# ---------------------------------------------------------------------------
# Failure handling + rebuild
# ---------------------------------------------------------------------------


class TestFailureAndRebuild:
    def test_mid_stream_failure_keeps_history_and_invalidates_link(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_NETWORK, message="conn reset"
        )
        backend = FakeBackend(
            turns=[
                _turn("A1", "resp-1"),
                [MessageStarted(), TextDelta("par"), failure],
                _turn("A2", "resp-2"),
            ]
        )
        store = ConversationStore()
        client = _client(backend, store)

        # Turn 1 commits [user one, assistant A1].
        client.post("/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH)

        # Turn 2 matches, reuses the session, fails mid-stream.
        lines = _stream_all(
            client,
            _chat([_user("one"), _assistant("A1"), _user("two")], stream=True),
        )
        assert "data: [DONE]" not in lines

        conversation = _single_conversation(store)
        assert conversation.messages == [
            CanonicalMessage(role="user", content="one"),
            CanonicalMessage(role="assistant", content="A1"),
        ]  # partial text is NOT stored (commit-on-finish)
        assert conversation.backend_session_id is None  # link invalidated
        assert conversation.backend_parent_message_id is None

        # Turn 3: the client re-sends (it never saw a reply). History still
        # matches, but the dead link forces a REBUILD — fresh session and a
        # full-history prompt reconstructed from canonical state.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A2"

        assert [s.session_id for s in backend.sessions_created] == [
            "fake-session-1",
            "fake-session-2",
        ]
        rebuild = backend.turn_calls[2]
        assert rebuild.session_id == "fake-session-2"
        assert rebuild.parent_message_id is None
        assert rebuild.prompt == "[user]\none\n\n[assistant]\nA1\n\n[user]\ntwo"

        conversation = _single_conversation(store)
        assert conversation.backend_session_id == "fake-session-2"
        assert conversation.backend_parent_message_id == "resp-2"
        assert conversation.messages[-1] == CanonicalMessage(
            role="assistant", content="A2"
        )

    def test_pre_stream_failure_on_existing_conversation_invalidates_link(
        self,
    ) -> None:
        # M9 (ADR-036): RATE_LIMITED is retryable, so the failing turn must
        # be scripted once per attempt in the bounded budget (1 + 2 retries;
        # backoff zeroed — the schedule is pinned in
        # tests/test_m9_reliability.py). The invalidation/rebuild contract
        # is unchanged: after the WHOLE budget fails, the link is gone and
        # the next request rebuilds from canonical state.
        failure = BackendFailure(
            category=BackendErrorCategory.RATE_LIMITED, message="slow down"
        )
        backend = FakeBackend(
            turns=[
                _turn("A1", "resp-1"),
                [failure],
                [failure],
                [failure],
                _turn("A2", "resp-2"),
            ]
        )
        store = ConversationStore()
        client = _client(backend, store, settings=_settings(retry_backoff_seconds=0.0))

        client.post("/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH)
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 429
        assert failed.json()["error"]["code"] == "RATE_LIMITED"

        conversation = _single_conversation(store)
        assert conversation.messages == [
            CanonicalMessage(role="user", content="one"),
            CanonicalMessage(role="assistant", content="A1"),
        ]
        assert conversation.backend_session_id is None

        retry = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert retry.status_code == 200
        rebuild = backend.turn_calls[4]  # three failed attempts consumed 1..3
        assert rebuild.session_id == "fake-session-2"
        assert "[user]\none" in rebuild.prompt  # rebuilt from canonical state
        assert "[user]\ntwo" in rebuild.prompt


# ---------------------------------------------------------------------------
# Reconstruction (the M4 exit criterion)
# ---------------------------------------------------------------------------


class TestReconstruction:
    def test_a_rebuilt_store_continues_the_same_backend_session(self) -> None:
        # Simulated restart/failover: canonical state leaves the process as
        # a plain dict snapshot and is re-injected into a fresh store. The
        # follow-up request must resolve onto the SAME backend session with
        # the SAME parent — proving local reconstructability end-to-end.
        backend = FakeBackend(turns=[_turn("A1", "resp-1"), _turn("A2", "resp-2")])

        store1 = ConversationStore()
        client1 = _client(backend, store1)
        response = client1.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 200

        snapshot = json.loads(json.dumps(_single_conversation(store1).to_dict()))

        store2 = ConversationStore()
        store2.put(Conversation.from_dict(snapshot))
        client2 = _client(backend, store2)
        response = client2.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A2"

        # No new backend session was needed — the rebuilt link was reused.
        assert [s.session_id for s in backend.sessions_created] == ["fake-session-1"]
        assert backend.turn_calls[1].session_id == "fake-session-1"
        assert backend.turn_calls[1].parent_message_id == "resp-1"
        assert backend.turn_calls[1].prompt == "[user]\ntwo"
