"""M1 tests: FakeBackend deterministic behavior."""

from __future__ import annotations

import pytest

from app.backends import FakeBackend
from app.backends.fake import TurnCall, fake_text_turn
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import (
    MessageFinished,
    MessageStarted,
    ReasoningDelta,
    TextDelta,
)


class TestScriptedTurns:
    def test_replays_scripted_events_in_order_across_turns(self) -> None:
        backend = FakeBackend(
            turns=[
                [MessageStarted(), TextDelta("Hel"), TextDelta("lo"), MessageFinished("stop")],
                [ReasoningDelta("hm"), TextDelta("42"), MessageFinished("stop")],
            ]
        )
        first = list(backend.stream_turn("s1", "one"))
        second = list(backend.stream_turn("s1", "two"))
        assert first == [
            MessageStarted(),
            TextDelta("Hel"),
            TextDelta("lo"),
            MessageFinished("stop"),
        ]
        assert second == [
            ReasoningDelta("hm"),
            TextDelta("42"),
            MessageFinished("stop"),
        ]

    def test_empty_scripted_turn_yields_nothing(self) -> None:
        backend = FakeBackend(turns=[[]])
        assert list(backend.stream_turn("s1", "p")) == []

    def test_scripted_exception_is_raised(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.RATE_LIMITED, message="slow down"
        )
        backend = FakeBackend(turns=[[TextDelta("partial"), failure]])
        with pytest.raises(BackendFailure) as excinfo:
            list(backend.stream_turn("s1", "p"))
        assert excinfo.value is failure
        assert excinfo.value.retryable is True

    def test_exhausted_script_raises_internal_failure(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn()])
        list(backend.stream_turn("s1", "p"))  # consumes the only turn
        with pytest.raises(BackendFailure) as excinfo:
            list(backend.stream_turn("s1", "again"))
        assert excinfo.value.category is BackendErrorCategory.INTERNAL

    def test_no_script_at_all_raises_internal_failure(self) -> None:
        backend = FakeBackend()
        with pytest.raises(BackendFailure) as excinfo:
            list(backend.stream_turn("s1", "p"))
        assert excinfo.value.category is BackendErrorCategory.INTERNAL


class TestCallRecording:
    def test_records_every_call_with_all_arguments(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn(), fake_text_turn()])
        list(backend.stream_turn("sess-A", "first"))
        list(
            backend.stream_turn(
                "sess-B",
                "second",
                parent_message_id="parent-1",
                thinking_enabled=True,
                search_enabled=True,
            )
        )
        assert backend.turn_calls == [
            TurnCall("sess-A", "first", None, False, False),
            TurnCall("sess-B", "second", "parent-1", True, True),
        ]

    def test_call_is_recorded_even_when_script_raises(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_5XX, message="boom"
        )
        backend = FakeBackend(turns=[[failure]])
        with pytest.raises(BackendFailure):
            list(backend.stream_turn("s1", "p"))
        assert len(backend.turn_calls) == 1


class TestSessionAndHealth:
    def test_create_session_returns_sequential_ids(self) -> None:
        backend = FakeBackend()
        s1 = backend.create_session()
        s2 = backend.create_session()
        assert s1.session_id == "fake-session-1"
        assert s2.session_id == "fake-session-2"
        assert backend.sessions_created == [s1, s2]

    def test_health_check_shape(self) -> None:
        health = FakeBackend().health_check()
        assert health.backend_type == "fake"
        assert health.ready is True
        assert health.details == {}


class TestHelpers:
    def test_fake_text_turn_is_a_coherent_text_turn(self) -> None:
        turn = fake_text_turn("hello")
        assert turn == [
            MessageStarted(),
            TextDelta("hello"),
            MessageFinished("stop"),
        ]
