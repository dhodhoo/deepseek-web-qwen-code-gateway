"""M3 tests: normalized event → OpenAI SSE chunk translator (unit level)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import (
    BackendError,
    BackendMessageId,
    MessageFinished,
    MessageStarted,
    ReasoningDelta,
    TextDelta,
    UnknownDelta,
)
from app.streaming import (
    SSE_DONE,
    STREAM_EMPTY,
    backend_event_to_chunk,
    sse_data_line,
    sse_stream,
)

META = {"chunk_id": "chatcmpl_local_test", "created": 123, "model": "deepseek-web"}


def _collect(gen) -> list[str]:
    async def run() -> list[str]:
        return [line async for line in gen]

    return asyncio.run(run())


def _parse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


class TestEventToChunk:
    def test_message_started_maps_to_role_delta(self) -> None:
        assert backend_event_to_chunk(MessageStarted()) == (
            {"role": "assistant", "content": ""},
            None,
        )

    def test_text_delta_maps_to_content_delta(self) -> None:
        assert backend_event_to_chunk(TextDelta("hi")) == ({"content": "hi"}, None)

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [("stop", "stop"), ("length", "length"), (None, "stop"), ("weird", "stop")],
    )
    def test_finish_reason_mapping(self, reason: str | None, expected: str) -> None:
        delta, finish = backend_event_to_chunk(MessageFinished(reason))
        assert delta == {}
        assert finish == expected

    @pytest.mark.parametrize(
        "event",
        [
            ReasoningDelta("internal thinking"),
            BackendMessageId("backend-id-1"),
            UnknownDelta("patch", "raw upstream junk"),
        ],
    )
    def test_vendor_internal_events_render_nothing(self, event) -> None:
        assert backend_event_to_chunk(event) is None


class TestSseFraming:
    def test_line_format(self) -> None:
        line = sse_data_line({"a": 1})
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        assert json.loads(line[len("data: ") :]) == {"a": 1}

    def test_utf8_is_not_ascii_escaped(self) -> None:
        line = sse_data_line({"content": "héllo"})
        assert "héllo" in line


class TestSseStream:
    def test_happy_path_shape(self) -> None:
        events = iter([TextDelta("Hel"), TextDelta("lo!"), MessageFinished("stop")])
        lines = _collect(sse_stream(MessageStarted(), events, **META))

        # role chunk + 2 content chunks + finish chunk + [DONE]
        assert len(lines) == 5
        chunks = [_parse(line) for line in lines[:-1]]
        assert lines[-1] == SSE_DONE

        for chunk in chunks:
            assert chunk["object"] == "chat.completion.chunk"
            assert chunk["id"] == "chatcmpl_local_test"
            assert chunk["created"] == 123
            assert chunk["model"] == "deepseek-web"
            assert len(chunk["choices"]) == 1
            assert chunk["choices"][0]["index"] == 0

        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        assert chunks[0]["choices"][0]["finish_reason"] is None
        assert chunks[1]["choices"][0]["delta"] == {"content": "Hel"}
        assert chunks[2]["choices"][0]["delta"] == {"content": "lo!"}
        assert chunks[3]["choices"][0]["delta"] == {}
        assert chunks[3]["choices"][0]["finish_reason"] == "stop"

    def test_role_is_injected_when_backend_skips_message_started(self) -> None:
        lines = _collect(sse_stream(TextDelta("x"), iter([]), **META))
        first = _parse(lines[0])
        assert first["choices"][0]["delta"] == {"role": "assistant", "content": "x"}
        assert lines[-1] == SSE_DONE

    def test_vendor_internal_events_never_reach_the_wire(self) -> None:
        events = iter(
            [
                ReasoningDelta("internal-thinking"),
                UnknownDelta("patch", "raw-upstream-junk"),
                BackendMessageId("backend-id-1"),
                TextDelta("visible"),
                MessageFinished("stop"),
            ]
        )
        lines = _collect(sse_stream(MessageStarted(), events, **META))
        joined = "".join(lines)
        assert "internal-thinking" not in joined
        assert "raw-upstream-junk" not in joined
        assert "backend-id-1" not in joined
        # Exactly: role chunk, one content chunk, finish chunk, [DONE].
        assert len(lines) == 4
        assert _parse(lines[1])["choices"][0]["delta"] == {"content": "visible"}

    def test_backend_error_event_becomes_error_envelope_without_done(self) -> None:
        events = iter(
            [BackendError(kind="RATE_LIMITED", retryable=True, message="slow down")]
        )
        lines = _collect(sse_stream(MessageStarted(), events, **META))
        assert len(lines) == 2
        assert _parse(lines[0])["choices"][0]["delta"]["role"] == "assistant"
        error = _parse(lines[1])["error"]
        assert error["code"] == "RATE_LIMITED"
        assert error["message"] == "slow down"
        assert SSE_DONE not in lines

    def test_backend_error_event_with_unknown_kind_maps_to_internal(self) -> None:
        events = iter(
            [BackendError(kind="NOT_A_CATEGORY", retryable=False, message="x")]
        )
        lines = _collect(sse_stream(MessageStarted(), events, **META))
        assert _parse(lines[-1])["error"]["code"] == "INTERNAL"

    def test_mid_stream_backend_failure_closes_without_done(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_NETWORK, message="conn reset"
        )
        events = iter([TextDelta("par"), failure])
        lines = _collect(sse_stream(MessageStarted(), events, **META))
        assert lines[-1] != SSE_DONE
        assert SSE_DONE not in lines
        assert _parse(lines[-1])["error"]["code"] == "UPSTREAM_NETWORK"
        assert _parse(lines[1])["choices"][0]["delta"]["content"] == "par"

    def test_empty_turn_emits_well_formed_empty_completion(self) -> None:
        lines = _collect(sse_stream(STREAM_EMPTY, iter([]), **META))
        assert len(lines) == 3
        assert _parse(lines[0])["choices"][0]["delta"] == {
            "role": "assistant",
            "content": "",
        }
        assert _parse(lines[1])["choices"][0]["finish_reason"] == "stop"
        assert lines[-1] == SSE_DONE

    def test_aclose_stops_the_stream_without_done(self) -> None:
        """Client disconnect semantics: closing the generator mid-stream
        stops emission cleanly; no further chunks and no [DONE] follow."""
        events = iter([TextDelta("a"), TextDelta("b"), MessageFinished("stop")])
        gen = sse_stream(MessageStarted(), events, **META)

        async def run() -> tuple[str, list[str]]:
            first = await gen.__anext__()
            await gen.aclose()
            return first, [line async for line in gen]

        first, rest = asyncio.run(run())
        assert first.startswith("data: ")
        assert rest == []
