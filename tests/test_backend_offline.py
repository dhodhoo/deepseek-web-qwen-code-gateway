"""Offline tests for DeepSeekWebBackend spike behavior.

No network access: the vendored client's ``chat_completion`` is stubbed at
the instance level. These tests prove that the backend boundary yields
normalized events, restores its raw-capture seam, and maps upstream
exceptions to the taxonomy.
"""

from __future__ import annotations

import pytest
from dsk.api import AuthenticationError, RateLimitError

from app.backends.deepseek_web import DeepSeekWebBackend
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import BackendMessageId, MessageFinished, TextDelta


@pytest.fixture()
def backend() -> DeepSeekWebBackend:
    # Dummy token: __init__ only validates non-empty and builds the PoW solver.
    return DeepSeekWebBackend("dummy-offline-token")


class TestBackendOffline:
    def test_health_check_shape(self, backend: DeepSeekWebBackend) -> None:
        health = backend.health_check()
        assert health.backend_type == "deepseek_web"
        assert health.ready is True
        assert "cookies_loaded" in health.details

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(BackendFailure) as excinfo:
            DeepSeekWebBackend("")
        assert excinfo.value.category is BackendErrorCategory.AUTH_INVALID

    def test_stream_turn_normalizes_stubbed_chunks(self, backend: DeepSeekWebBackend) -> None:
        canned = [
            {"content": "Hel", "type": "text", "finish_reason": None},
            {"content": "lo", "type": "text", "finish_reason": None},
            {"content": "", "type": "text", "finish_reason": "stop"},
        ]

        def fake_chat_completion(*args, **kwargs):
            yield from canned

        backend._api.chat_completion = fake_chat_completion  # type: ignore[method-assign]

        events = list(backend.stream_turn("sess-1", "hi"))
        assert events == [
            TextDelta("Hel"),
            TextDelta("lo"),
            MessageFinished("stop"),
        ]

    def test_raw_sink_captures_lines_and_seam_is_restored(
        self, backend: DeepSeekWebBackend
    ) -> None:
        def fake_chat_completion(*args, **kwargs):
            yield {"content": "x", "type": "text", "finish_reason": "stop"}

        backend._api.chat_completion = fake_chat_completion  # type: ignore[method-assign]
        assert "_parse_chunk" not in backend._api.__dict__

        sink: list[bytes] = []
        events = list(backend.stream_turn("sess-1", "hi", raw_sink=sink))
        assert events == [TextDelta("x"), MessageFinished("stop")]
        # Exact state restoration: no lingering instance attribute, and the
        # resolved bound method equals the class method again.
        assert "_parse_chunk" not in backend._api.__dict__
        assert backend._api._parse_chunk.__func__.__name__ == "_parse_chunk"

    def test_raw_sink_captures_when_parser_runs(self, backend: DeepSeekWebBackend) -> None:
        # Exercise the capture+adapt seam around the CURRENT protocol by
        # feeding raw lines through stream_turn's wrapped parser.
        sink: list[bytes] = []
        raw_lines = (
            b"event: ready",
            b'data: {"request_message_id": "r1", "response_message_id": "m1", "model_type": "default"}',
            b'data: {"p": "response/content", "o": "APPEND", "v": "ok"}',
            b'data: {"p": "response/status", "v": "FINISHED"}',
        )

        def fake_chat_completion(*args, **kwargs):
            # Emulate the vendored loop: every iter_lines() row goes through
            # the instance parser.
            for line in raw_lines:
                parsed = backend._api._parse_chunk(line)
                if parsed:
                    yield parsed

        backend._api.chat_completion = fake_chat_completion  # type: ignore[method-assign]
        events = list(backend.stream_turn("sess-1", "hi", raw_sink=sink))
        assert events == [
            BackendMessageId("m1"),
            TextDelta("ok"),
            MessageFinished("stop"),
        ]
        assert sink == list(raw_lines)
        assert "_parse_chunk" not in backend._api.__dict__

    def test_upstream_rate_limit_mapped(self, backend: DeepSeekWebBackend) -> None:
        def fake_chat_completion(*args, **kwargs):
            raise RateLimitError("API rate limit exceeded")
            yield  # pragma: no cover (make it a generator)

        backend._api.chat_completion = fake_chat_completion  # type: ignore[method-assign]

        with pytest.raises(BackendFailure) as excinfo:
            list(backend.stream_turn("sess-1", "hi"))
        assert excinfo.value.category is BackendErrorCategory.RATE_LIMITED
        assert excinfo.value.retryable is True

    def test_upstream_auth_mapped_mid_stream(self, backend: DeepSeekWebBackend) -> None:
        def fake_chat_completion(*args, **kwargs):
            yield {"content": "partial", "type": "text", "finish_reason": None}
            raise AuthenticationError("expired")

        backend._api.chat_completion = fake_chat_completion  # type: ignore[method-assign]

        with pytest.raises(BackendFailure) as excinfo:
            list(backend.stream_turn("sess-1", "hi"))
        assert excinfo.value.category is BackendErrorCategory.AUTH_INVALID

    def test_create_session_invalid_response_mapped(self, backend: DeepSeekWebBackend) -> None:
        def fake_create():
            raise RateLimitError("slow down")

        backend._api.create_chat_session = fake_create  # type: ignore[method-assign]
        with pytest.raises(BackendFailure) as excinfo:
            backend.create_session()
        assert excinfo.value.category is BackendErrorCategory.RATE_LIMITED

    def test_cookies_file_loading(self, tmp_path) -> None:
        cookies_file = tmp_path / "cookies.json"
        cookies_file.write_text('{"cookies": {"cf_clearance": "placeholder"}}')
        backend = DeepSeekWebBackend("dummy", cookies_file=cookies_file)
        assert backend.health_check().details["cookies_loaded"] is True
        # The cookie value itself must be held, but never appear in health output.
        assert "placeholder" not in str(backend.health_check())

    def test_bad_cookies_file_rejected(self, tmp_path) -> None:
        cookies_file = tmp_path / "cookies.json"
        cookies_file.write_text('{"no_cookies_key": true}')
        with pytest.raises(BackendFailure) as excinfo:
            DeepSeekWebBackend("dummy", cookies_file=cookies_file)
        assert excinfo.value.category is BackendErrorCategory.CLIENT_BAD_REQUEST
