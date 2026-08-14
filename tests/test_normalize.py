"""Offline tests for normalization of upstream data into internal events."""

from __future__ import annotations

import pytest

from app.backends.deepseek_web.normalize import (
    chunk_dict_to_events,
    normalize_stream_chunks,
    parse_sse_line,
    payload_to_events,
)
from app.backends.events import (
    BackendMessageId,
    MessageFinished,
    ReasoningDelta,
    TextDelta,
    UnknownDelta,
)


class TestChunkDictToEvents:
    """Normalization of the vendored client's reduced chunk dicts."""

    def test_text_chunk(self) -> None:
        events = chunk_dict_to_events(
            {"content": "hello", "type": "text", "finish_reason": None}
        )
        assert events == [TextDelta("hello")]

    def test_thinking_chunk(self) -> None:
        events = chunk_dict_to_events(
            {"content": "hmm", "type": "thinking", "finish_reason": None}
        )
        assert events == [ReasoningDelta("hmm")]

    def test_missing_type_defaults_to_text(self) -> None:
        events = chunk_dict_to_events({"content": "x", "finish_reason": None})
        assert events == [TextDelta("x")]

    def test_unknown_type_is_preserved(self) -> None:
        events = chunk_dict_to_events(
            {"content": "ref", "type": "search_result", "finish_reason": None}
        )
        assert events == [UnknownDelta(kind="search_result", content="ref")]

    def test_empty_content_produces_no_delta(self) -> None:
        assert chunk_dict_to_events({"content": "", "type": "text"}) == []

    def test_finish_only_chunk(self) -> None:
        events = chunk_dict_to_events(
            {"content": "", "type": "text", "finish_reason": "stop"}
        )
        assert events == [MessageFinished("stop")]

    def test_missing_keys_tolerated(self) -> None:
        assert chunk_dict_to_events({}) == []

    def test_text_and_finish_in_same_chunk(self) -> None:
        events = chunk_dict_to_events(
            {"content": "done", "type": "text", "finish_reason": "stop"}
        )
        assert events == [TextDelta("done"), MessageFinished("stop")]


class TestPayloadToEvents:
    """Normalization of full raw SSE payloads."""

    def test_full_text_payload(self) -> None:
        payload = {
            "id": "m1",
            "choices": [
                {"index": 0, "delta": {"content": "hi", "type": "text"}, "finish_reason": None}
            ],
        }
        events = payload_to_events(payload)
        assert events == [BackendMessageId("m1"), TextDelta("hi")]

    def test_payload_id_reported_once_per_payload(self) -> None:
        events = payload_to_events({"id": "m9", "choices": []})
        assert events == [BackendMessageId("m9")]

    def test_no_choices_key(self) -> None:
        assert payload_to_events({"heartbeat": True}) == []

    def test_empty_choices_list(self) -> None:
        assert payload_to_events({"choices": []}) == []

    def test_choices_not_a_list(self) -> None:
        assert payload_to_events({"choices": "weird"}) == []

    def test_non_dict_choice_skipped(self) -> None:
        assert payload_to_events({"choices": [None, "x"]}) == []

    def test_choice_without_delta_but_with_finish(self) -> None:
        events = payload_to_events({"choices": [{"finish_reason": "stop"}]})
        assert events == [MessageFinished("stop")]

    def test_multiple_choices(self) -> None:
        payload = {
            "choices": [
                {"delta": {"content": "a", "type": "text"}, "finish_reason": None},
                {"delta": {"content": "b", "type": "thinking"}, "finish_reason": None},
            ]
        }
        assert payload_to_events(payload) == [TextDelta("a"), ReasoningDelta("b")]


class TestFixtureStreams:
    """End-to-end offline parsing of fixture streams (raw line -> events)."""

    @staticmethod
    def _events_from_lines(lines):
        events = []
        for line in lines:
            payload = parse_sse_line(line)
            if payload is not None:
                events.extend(payload_to_events(payload))
        return events

    def test_text_only_stream(self, read_fixture) -> None:
        events = self._events_from_lines(read_fixture("stream_text_only.sse.txt"))
        deltas = [e for e in events if isinstance(e, TextDelta)]
        finishes = [e for e in events if isinstance(e, MessageFinished)]
        assert "".join(d.text for d in deltas) == "OK"
        assert finishes == [MessageFinished("stop")]

    def test_thinking_then_text_stream(self, read_fixture) -> None:
        events = self._events_from_lines(
            read_fixture("stream_thinking_then_text.sse.txt")
        )
        thinking = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert thinking == "Let me consider the arithmetic."
        assert text == "2+2 equals 4."
        # Empty lines between SSE lines must be tolerated.
        assert events[-1] == MessageFinished("stop")

    def test_noise_stream_tolerated_and_unknown_preserved(self, read_fixture) -> None:
        events = self._events_from_lines(read_fixture("stream_noise.sse.txt"))
        kinds = [type(e) for e in events]
        assert TextDelta in kinds
        assert MessageFinished in kinds
        unknown = [e for e in events if isinstance(e, UnknownDelta)]
        assert unknown == [UnknownDelta(kind="search_result", content="search result ref")]
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "visible text"

    def test_normalize_stream_chunks_helper(self) -> None:
        chunks = [
            {"content": "a", "type": "text", "finish_reason": None},
            {"content": "b", "type": "text", "finish_reason": None},
            {"content": "", "type": "text", "finish_reason": "stop"},
        ]
        events = list(normalize_stream_chunks(chunks))
        assert events == [TextDelta("a"), TextDelta("b"), MessageFinished("stop")]

    def test_malformed_stream_raises_at_bad_line(self, read_fixture) -> None:
        """A syntactically broken data line is fatal (vendored parity); the
        lines before it still parse cleanly."""
        from app.backends.deepseek_web.normalize import RawStreamParseError

        lines = read_fixture("stream_malformed.sse.txt")
        parsed_events = []
        with pytest.raises(RawStreamParseError):
            for line in lines:
                payload = parse_sse_line(line)
                if payload is not None:
                    parsed_events.extend(payload_to_events(payload))
        # The first (valid) line was consumed before the failure.
        assert TextDelta("partial") in parsed_events
