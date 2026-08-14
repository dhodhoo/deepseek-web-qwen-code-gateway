"""Offline tests for fixture sanitization (credential/identity hygiene)."""

from __future__ import annotations

import json

from app.backends.deepseek_web.sanitize import (
    REMOVED_KEYS,
    sanitize_payload,
    sanitize_raw_sse_lines,
)


class TestSanitizeRawSseLines:
    def test_identifiers_become_stable_placeholders(self) -> None:
        lines = [
            'data: {"id": "real-1", "message_id": "msg-A", "choices": []}',
            'data: {"id": "real-1", "parent_message_id": "msg-A", "choices": []}',
        ]
        out = sanitize_raw_sse_lines(lines)
        first = json.loads(out[0][len("data: "):])
        second = json.loads(out[1][len("data: "):])
        assert first["id"] == second["id"] == "SANITIZED-ID-1"
        assert first["message_id"] == "SANITIZED-ID-2"
        assert second["parent_message_id"] == "SANITIZED-ID-2"
        assert "real-1" not in out[0] + out[1]
        assert "msg-A" not in out[0] + out[1]

    def test_removed_keys_are_dropped_including_nested(self) -> None:
        lines = [
            'data: {"user": {"nickname": "bob"}, "token": "secret", '
            '"choices": [{"delta": {"content": "hi", "cookie": "cf_clearance=x"}}]}'
        ]
        out = sanitize_raw_sse_lines(lines)
        payload = json.loads(out[0][len("data: "):])
        for key in ("user", "token"):
            assert key not in payload
        assert "cookie" not in payload["choices"][0]["delta"]
        assert payload["choices"][0]["delta"]["content"] == "hi"

    def test_every_removed_key_is_actually_removed(self) -> None:
        payload = {key: "x" for key in REMOVED_KEYS}
        payload["keep"] = "y"
        assert sanitize_payload(payload) == {"keep": "y"}

    def test_non_data_lines_pass_through(self) -> None:
        lines = ["", ": keepalive", "event: message"]
        assert sanitize_raw_sse_lines(lines) == ["", ": keepalive", "event: message"]

    def test_malformed_data_line_passes_through(self) -> None:
        lines = ["data: {not json"]
        assert sanitize_raw_sse_lines(lines) == ["data: {not json"]

    def test_non_object_data_passes_through(self) -> None:
        lines = ["data: [1, 2, 3]"]
        assert sanitize_raw_sse_lines(lines) == ["data: [1, 2, 3]"]

    def test_bytes_input_accepted(self) -> None:
        out = sanitize_raw_sse_lines([b'data: {"id": "abc", "choices": []}'])
        assert "abc" not in out[0]
        assert "SANITIZED-ID-1" in out[0]

    def test_content_preserved_verbatim(self) -> None:
        lines = ['data: {"choices": [{"delta": {"content": "2+2=4", "type": "text"}}]}']
        out = sanitize_raw_sse_lines(lines)
        payload = json.loads(out[0][len("data: "):])
        assert payload["choices"][0]["delta"]["content"] == "2+2=4"

    def test_fixture_stream_with_ids_sanitizes(self, read_fixture) -> None:
        out = sanitize_raw_sse_lines(read_fixture("stream_with_ids.sse.txt"))
        joined = "\n".join(out)
        assert "SYN-USER-ID-3" not in joined
        assert "SYN-MSG-ID-77" not in joined
        assert "nickname" not in joined
        assert "hello" in joined  # model content preserved
