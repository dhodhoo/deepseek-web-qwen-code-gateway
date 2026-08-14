"""Offline tests for raw DeepSeek SSE line parsing (M0).

Covers the unit cases required by docs/TEST_PLAN.md for backend
normalization: text chunk, thinking chunk, stop chunk, empty line,
malformed JSON, unexpected event fields.
"""

from __future__ import annotations

import pytest

from app.backends.deepseek_web.normalize import (
    RawStreamParseError,
    parse_sse_line,
)


class TestParseSseLine:
    def test_text_data_line(self) -> None:
        payload = parse_sse_line(
            b'data: {"choices": [{"delta": {"content": "hi", "type": "text"}, "finish_reason": null}]}'
        )
        assert payload is not None
        assert payload["choices"][0]["delta"]["content"] == "hi"

    def test_thinking_data_line(self) -> None:
        payload = parse_sse_line(
            b'data: {"choices": [{"delta": {"content": "hmm", "type": "thinking"}, "finish_reason": null}]}'
        )
        assert payload["choices"][0]["delta"]["type"] == "thinking"

    def test_stop_chunk(self) -> None:
        payload = parse_sse_line(
            b'data: {"choices": [{"delta": {"content": "", "type": "text"}, "finish_reason": "stop"}]}'
        )
        assert payload["choices"][0]["finish_reason"] == "stop"

    def test_empty_line_returns_none(self) -> None:
        assert parse_sse_line(b"") is None
        assert parse_sse_line(b"   ") is None

    def test_comment_and_other_sse_fields_return_none(self) -> None:
        assert parse_sse_line(b": keepalive") is None
        assert parse_sse_line(b"event: message") is None
        assert parse_sse_line(b"id: 42") is None

    def test_data_without_space_prefix_is_not_parsed(self) -> None:
        # Upstream parser contract: exact "data: " prefix.
        assert parse_sse_line(b'data:{"a": 1}') is None

    def test_accepts_str_input(self) -> None:
        payload = parse_sse_line('data: {"choices": []}')
        assert payload == {"choices": []}

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(RawStreamParseError):
            parse_sse_line(b'data: {"choices": [{"delta": BROKEN')

    def test_non_object_payload_is_tolerated(self) -> None:
        # Vendored-parser parity: non-object JSON (e.g. a hypothetical
        # "data: [DONE]") is ignored mid-stream, not fatal.
        assert parse_sse_line(b"data: [1, 2, 3]") is None
        assert parse_sse_line(b'data: "just a string"') is None
        assert parse_sse_line(b"data: null") is None

    def test_unexpected_fields_are_preserved(self) -> None:
        payload = parse_sse_line(
            b'data: {"id": "x", "unexpected_field": {"nested": true}, "choices": []}'
        )
        assert payload["unexpected_field"] == {"nested": True}
