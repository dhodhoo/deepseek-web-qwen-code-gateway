"""Offline tests for the current-protocol wire adapter (M0 live finding).

The primary fixture is the SANITIZED live capture written by the M0 probe
(``tests/fixtures/deepseek_web/live/stream_*.sse.txt``). Synthetic cases
cover the individual protocol constructs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.backends.deepseek_web.normalize import (
    RawStreamParseError,
    chunk_dict_to_events,
)
from app.backends.deepseek_web.wire import WireSession, adapt_raw_line
from app.backends.events import (
    BackendMessageId,
    MessageFinished,
    ReasoningDelta,
    TextDelta,
)

LIVE_DIR = Path(__file__).parent / "fixtures" / "deepseek_web" / "live"


def live_captures() -> list[Path]:
    return sorted(LIVE_DIR.glob("stream_*.sse.txt"))


class TestAdaptRawLineSynthetic:
    def test_event_lines_and_blanks_are_ignored(self) -> None:
        assert adapt_raw_line(b"event: ready") is None
        assert adapt_raw_line(b"event: finish") is None
        assert adapt_raw_line(b"") is None
        assert adapt_raw_line(b"   ") is None
        assert adapt_raw_line(b": keepalive") is None

    def test_ready_event_yields_message_ids(self) -> None:
        chunk = adapt_raw_line(
            b'data: {"request_message_id": "req-1", "response_message_id": "resp-1", "model_type": "default"}'
        )
        assert chunk is not None
        assert chunk["response_message_id"] == "resp-1"
        assert chunk["request_message_id"] == "req-1"
        assert chunk["finish_reason"] is None

    def test_snapshot_yields_message_id_only(self) -> None:
        chunk = adapt_raw_line(
            b'data: {"v": {"response": {"message_id": "resp-1", "parent_id": "req-1", '
            b'"role": "ASSISTANT", "status": "WIP", "content": ""}}}'
        )
        assert chunk is not None
        assert chunk["response_message_id"] == "resp-1"
        assert chunk["content"] == ""

    def test_content_append_becomes_text_delta(self) -> None:
        chunk = adapt_raw_line(
            b'data: {"p": "response/content", "o": "APPEND", "v": "Hello"}'
        )
        assert chunk == {"content": "Hello", "type": "text", "finish_reason": None}

    def test_thinking_append_becomes_thinking_delta(self) -> None:
        chunk = adapt_raw_line(
            b'data: {"p": "response/thinking_content", "o": "APPEND", "v": "hmm"}'
        )
        assert chunk == {"content": "hmm", "type": "thinking", "finish_reason": None}

    def test_set_on_content_path_is_not_a_delta(self) -> None:
        # Only APPEND ops are deltas in M0; a SET on content would be a
        # full replacement and must not be double-counted as streamed text.
        assert (
            adapt_raw_line(b'data: {"p": "response/content", "o": "SET", "v": "full"}')
            is None
        )

    def test_empty_append_ignored(self) -> None:
        assert adapt_raw_line(b'data: {"p": "response/content", "o": "APPEND", "v": ""}') is None

    def test_status_finished_maps_to_stop(self) -> None:
        chunk = adapt_raw_line(b'data: {"p": "response/status", "v": "FINISHED"}')
        assert chunk == {"content": "", "type": "", "finish_reason": "stop"}

    def test_status_wip_not_terminal(self) -> None:
        assert adapt_raw_line(b'data: {"p": "response/status", "v": "WIP"}') is None

    def test_bookkeeping_paths_ignored(self) -> None:
        assert (
            adapt_raw_line(b'data: {"p": "response/accumulated_token_usage", "o": "SET", "v": 39}')
            is None
        )
        assert adapt_raw_line(b"data: {}") is None  # event: finish payload
        assert adapt_raw_line(b'data: {"content": "OK"}') is None  # event: title
        assert (
            adapt_raw_line(b'data: {"click_behavior": "none", "auto_resume": false}')
            is None  # event: close
        )
        assert adapt_raw_line(b'data: {"updated_at": 1786711092.380996}') is None

    def test_non_object_data_ignored(self) -> None:
        assert adapt_raw_line(b"data: [1, 2]") is None

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(RawStreamParseError):
            adapt_raw_line(b'data: {"p": "response/content", BROKEN')


class TestStickyPath:
    """The current protocol omits `p` (and sometimes `o`) after it is set."""

    @staticmethod
    def _events_from_lines(lines):
        session = WireSession()
        events = []
        for line in lines:
            chunk = session.adapt(line)
            if chunk:
                events.extend(chunk_dict_to_events(chunk))
        return events

    def test_sticky_path_appends_without_p(self) -> None:
        lines = [
            b'data: {"p": "response/thinking_content", "v": "1"}',
            b'data: {"o": "APPEND", "v": "."}',
            b'data: {"v": " The"}',
        ]
        events = self._events_from_lines(lines)
        assert events == [ReasoningDelta("1"), ReasoningDelta("."), ReasoningDelta(" The")]

    def test_path_switch_is_respected(self) -> None:
        lines = [
            b'data: {"p": "response/thinking_content", "v": "think"}',
            b'data: {"v": " more"}',
            b'data: {"p": "response/content", "o": "APPEND", "v": "answer"}',
            b'data: {"v": " tail"}',
        ]
        events = self._events_from_lines(lines)
        assert events == [
            ReasoningDelta("think"),
            ReasoningDelta(" more"),
            TextDelta("answer"),
            TextDelta(" tail"),
        ]

    def test_set_not_treated_as_delta_even_sticky(self) -> None:
        lines = [
            b'data: {"p": "response/content", "o": "SET", "v": "full"}',
            b'data: {"o": "SET", "v": "again"}',
        ]
        assert self._events_from_lines(lines) == []

    def test_status_without_op_still_terminal(self) -> None:
        lines = [
            b'data: {"p": "response/content", "o": "APPEND", "v": "x"}',
            b'data: {"p": "response/status", "v": "FINISHED"}',
        ]
        events = self._events_from_lines(lines)
        assert events[-1] == MessageFinished("stop")

    def test_no_path_context_yields_nothing(self) -> None:
        # Bare op before any p was ever set: no context, ignore.
        session = WireSession()
        assert session.adapt(b'data: {"o": "APPEND", "v": "orphan"}') is None

    def test_thinking_capture_reconstructs_full_reasoning(self) -> None:
        thinking_captures = [c for c in live_captures() if _has_thinking(c)]
        assert thinking_captures, "expected a thinking-enabled live capture"
        events = self._events_from_lines(
            thinking_captures[0].read_text(encoding="utf-8").splitlines()
        )
        reasoning = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert "arithmetic" in reasoning
        assert text.strip() == "4"
        assert [e for e in events if isinstance(e, MessageFinished)][
            -1
        ].finish_reason == "stop"


def _has_thinking(capture: Path) -> bool:
    return "response/thinking_content" in capture.read_text(encoding="utf-8")


class TestAdaptLiveCapture:
    """Invariant checks over every sanitized live capture from the probe."""

    def test_live_captures_exist(self) -> None:
        assert live_captures(), (
            "expected at least one sanitized live capture in "
            "tests/fixtures/deepseek_web/live/ (run scripts/probe_deepseek.py)"
        )

    @pytest.mark.parametrize(
        "capture", live_captures(), ids=lambda p: p.name
    )
    def test_capture_streams_to_terminal_events(self, capture: Path) -> None:
        lines = capture.read_text(encoding="utf-8").splitlines()
        session = WireSession()
        events = []
        for line in lines:
            chunk = session.adapt(line)
            if chunk:
                events.extend(chunk_dict_to_events(chunk))

        texts = [e for e in events if isinstance(e, TextDelta)]
        finishes = [e for e in events if isinstance(e, MessageFinished)]
        ids = [e for e in events if isinstance(e, BackendMessageId)]

        assert "".join(t.text for t in texts).strip(), "capture must carry text"
        assert finishes, "capture must reach a terminal status"
        assert finishes[-1].finish_reason == "stop"
        assert ids, "capture must expose the response message id"

    def test_first_live_capture_matches_m0_expectation(self) -> None:
        captures = live_captures()
        first = captures[0]
        lines = first.read_text(encoding="utf-8").splitlines()
        session = WireSession()
        events = []
        for line in lines:
            chunk = session.adapt(line)
            if chunk:
                events.extend(chunk_dict_to_events(chunk))

        # M0 canned prompt: "Reply with exactly one word: OK"
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text.strip() == "OK"
