"""M3 tests: SSE streaming surface via TestClient + FakeBackend (offline)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pydantic import SecretStr

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
from app.backends.fake import FakeBackend
from app.config import GatewaySettings
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

SSE_TURN = [
    MessageStarted(),
    TextDelta("Hel"),
    TextDelta("lo!"),
    MessageFinished("stop"),
]


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(settings: GatewaySettings, backend: FakeBackend) -> TestClient:
    return TestClient(create_app(settings, backend))


def _chat_body(**overrides) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
    }
    body.update(overrides)
    return body


def _stream_lines(client: TestClient, payload: dict) -> list[str]:
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=AUTH
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return [line for line in response.iter_lines() if line.strip()]


def _parse(line: str) -> dict:
    assert line.startswith("data: "), f"unexpected SSE framing: {line!r}"
    return json.loads(line[len("data: ") :])


class TestStreamingHappyPath:
    def test_incremental_chunks_then_done(self) -> None:
        backend = FakeBackend(turns=[SSE_TURN])
        lines = _stream_lines(_client(_settings(), backend), _chat_body())

        assert all(line.startswith("data: ") for line in lines)
        assert lines[-1] == "data: [DONE]"

        chunks = [_parse(line) for line in lines[:-1]]
        ids = {chunk["id"] for chunk in chunks}
        assert len(ids) == 1
        assert ids.pop().startswith("chatcmpl_local_")
        assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}
        assert {chunk["model"] for chunk in chunks} == {MODEL}
        assert len({chunk["created"] for chunk in chunks}) == 1

        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        finishes = [chunk["choices"][0]["finish_reason"] for chunk in chunks]
        assert deltas[0] == {"role": "assistant", "content": ""}
        assert deltas[1] == {"content": "Hel"}
        assert deltas[2] == {"content": "lo!"}
        assert deltas[-1] == {}
        assert finishes[:-1] == [None] * (len(finishes) - 1)
        assert finishes[-1] == "stop"

        full_text = "".join(delta.get("content", "") for delta in deltas)
        assert full_text == "Hello!"

    def test_each_text_delta_is_its_own_chunk(self) -> None:
        turn = [MessageStarted()] + [TextDelta(f"w{i}") for i in range(5)] + [
            MessageFinished("stop")
        ]
        backend = FakeBackend(turns=[turn])
        lines = _stream_lines(_client(_settings(), backend), _chat_body())
        contents = [
            _parse(line)["choices"][0]["delta"].get("content")
            for line in lines
            if line != "data: [DONE]"
        ]
        assert contents[1:6] == ["w0", "w1", "w2", "w3", "w4"]

    def test_stream_options_include_usage_is_tolerated_without_usage_chunk(
        self,
    ) -> None:
        # DeepSeek Web exposes no token counts; the verified client tolerates
        # a missing usage chunk (docs/UPSTREAM_NOTES.md).
        backend = FakeBackend(turns=[SSE_TURN])
        lines = _stream_lines(
            _client(_settings(), backend),
            _chat_body(stream_options={"include_usage": True}),
        )
        assert lines[-1] == "data: [DONE]"
        assert all("usage" not in line for line in lines)

    def test_finish_reason_length_passes_through(self) -> None:
        backend = FakeBackend(
            turns=[[MessageStarted(), TextDelta("x"), MessageFinished("length")]]
        )
        lines = _stream_lines(_client(_settings(), backend), _chat_body())
        chunks = [_parse(line) for line in lines[:-1]]
        assert chunks[-1]["choices"][0]["finish_reason"] == "length"

    def test_repeated_identical_streaming_request_is_a_new_conversation(self) -> None:
        # M4 note: re-sending the same single user message is a duplicate
        # (stored history already contains the assistant reply), not a
        # continuation — so a fresh conversation/session is correct here
        # (ADR-020; true continuations reuse sessions, see
        # tests/test_api_multi_turn.py).
        backend = FakeBackend(turns=[SSE_TURN, SSE_TURN])
        client = _client(_settings(), backend)
        _stream_lines(client, _chat_body())
        _stream_lines(client, _chat_body())
        assert [s.session_id for s in backend.sessions_created] == [
            "fake-session-1",
            "fake-session-2",
        ]
        assert "[user]\nHello" in backend.turn_calls[0].prompt


class TestStreamingNoLeakage:
    def test_raw_upstream_and_vendor_internal_data_never_leak(self) -> None:
        turn = [
            MessageStarted(),
            ReasoningDelta("internal-thinking-secret"),
            UnknownDelta("patch", '{"raw":"upstream-junk"}'),
            BackendMessageId("backend-id-1"),
            TextDelta("visible"),
            MessageFinished("stop"),
        ]
        backend = FakeBackend(turns=[turn])
        lines = _stream_lines(_client(_settings(), backend), _chat_body())
        joined = "".join(lines)

        # Only OpenAI data lines, no upstream SSE framing (event: lines etc.)
        assert all(line.startswith("data: ") for line in lines)
        assert "event:" not in joined
        # Nothing vendor-internal crosses the wire.
        assert "internal-thinking-secret" not in joined
        assert "upstream-junk" not in joined
        assert "backend-id-1" not in joined
        # Exactly the visible text is delivered.
        contents = [
            _parse(line)["choices"][0]["delta"].get("content")
            for line in lines
            if line != "data: [DONE]"
        ]
        assert [c for c in contents if c] == ["visible"]


class TestStreamingErrors:
    def test_failure_before_first_byte_is_an_http_status(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.RATE_LIMITED, message="slow down"
        )
        client = _client(_settings(), FakeBackend(turns=[[failure]]))
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "RATE_LIMITED"
        assert error["message"] == "slow down"

    def test_backend_error_event_before_first_byte_is_an_http_status(self) -> None:
        event = BackendError(
            kind="UPSTREAM_5XX", retryable=True, message="boom", status_code=500
        )
        client = _client(_settings(), FakeBackend(turns=[[event]]))
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "UPSTREAM_5XX"

    def test_mid_stream_failure_emits_error_envelope_and_no_done(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_NETWORK, message="conn reset"
        )
        turn = [MessageStarted(), TextDelta("par"), failure]
        client = _client(_settings(), FakeBackend(turns=[turn]))
        with client.stream(
            "POST", "/v1/chat/completions", json=_chat_body(), headers=AUTH
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line.strip()]

        assert "data: [DONE]" not in lines
        last = _parse(lines[-1])
        assert last["error"]["code"] == "UPSTREAM_NETWORK"
        assert last["error"]["message"] == "conn reset"
        # The partial content still arrived before the failure.
        assert _parse(lines[1])["choices"][0]["delta"]["content"] == "par"

    def test_empty_scripted_turn_is_a_well_formed_empty_stream(self) -> None:
        client = _client(_settings(), FakeBackend(turns=[[]]))
        lines = _stream_lines(client, _chat_body())
        assert lines[-1] == "data: [DONE]"
        chunks = [_parse(line) for line in lines[:-1]]
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    def test_exhausted_fake_before_first_byte_is_500(self) -> None:
        client = _client(_settings(), FakeBackend())  # no scripted turns
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL"


class TestStreamingValidationAndAuth:
    def test_stream_with_tools_is_accepted_and_ignored_until_m6(self) -> None:
        # M5 (ADR-021): Qwen Code agent turns always carry tools[]; the
        # gateway streams a plain-text answer instead of rejecting.
        backend = FakeBackend(turns=[SSE_TURN])
        client = _client(_settings(), backend)
        payload = _chat_body(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "run",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            stream_options={"include_usage": True},
        )
        lines = _stream_lines(client, payload)
        assert lines[-1] == "data: [DONE]"
        chunks = [_parse(line) for line in lines[:-1]]
        full_text = "".join(
            chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
        )
        assert full_text == "Hello!"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        # tools are ignored, never echoed; no usage chunk is emitted and the
        # Qwen Code client tolerates its absence (UPSTREAM_NOTES, M5).
        assert all("tool_calls" not in chunk["choices"][0]["delta"] for chunk in chunks)
        assert all("usage" not in chunk for chunk in chunks)

    def test_stream_with_unknown_model_is_404(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions", json=_chat_body(model="gpt-4o"), headers=AUTH
        )
        assert response.status_code == 404

    def test_stream_requires_auth(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post("/v1/chat/completions", json=_chat_body())
        assert response.status_code == 401
