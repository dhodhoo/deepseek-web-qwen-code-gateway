"""M5 tests: Qwen Code wire fixtures drive the gateway surface (offline).

The fixture bodies live in ``tests/fixtures/qwen_code_wire/`` and are
synthesized from the source-verified Qwen Code wire facts in
docs/UPSTREAM_NOTES.md (see the fixture README for provenance). These
tests pin, per ROADMAP M5, that the exact current agent request/history
format is covered:

* agent turn with tools[] + stream_options     → plain-text 200 (tools
  accepted and ignored until M6, ADR-021);
* side query (stream: false, no tools)         → 200 non-stream JSON;
* tool-loop history (assistant tool_calls /
  role=tool)                                   → deterministic 400
  UNSUPPORTED_MESSAGE until M6 implements tool-history compilation;
* non-standard extra fields                    → lenient parsing, 200.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "qwen_code_wire"

AGENT_TURN = "The directory contains README.md, pyproject.toml and src."

AGENT_SSE_TURN = [
    MessageStarted(),
    TextDelta("The directory contains "),
    TextDelta("README.md, pyproject.toml and src."),
    MessageFinished("stop"),
]


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(settings: GatewaySettings, backend: FakeBackend) -> TestClient:
    return TestClient(create_app(settings, backend))


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


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


class TestAgentTurnStreamWithTools:
    """Fixture: agent_turn_stream_with_tools.json (the main agent shape)."""

    def test_streams_plain_text_answer(self) -> None:
        backend = FakeBackend(turns=[AGENT_SSE_TURN])
        client = _client(_settings(), backend)
        lines = _stream_lines(client, _load_fixture("agent_turn_stream_with_tools.json"))

        assert all(line.startswith("data: ") for line in lines)
        assert lines[-1] == "data: [DONE]"

        chunks = [_parse(line) for line in lines[:-1]]
        assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}
        full_text = "".join(
            chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
        )
        assert full_text == AGENT_TURN
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
        # tools[] are accepted and IGNORED (ADR-021): the stream contains
        # no tool_calls deltas and no usage chunk. Qwen Code tolerates the
        # missing usage chunk (docs/UPSTREAM_NOTES.md, M5 verification).
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        assert all("tool_calls" not in delta for delta in deltas)
        assert all("usage" not in chunk for chunk in chunks)

    def test_backend_receives_compiled_prompt_not_tool_noise(self) -> None:
        backend = FakeBackend(turns=[AGENT_SSE_TURN])
        client = _client(_settings(), backend)
        _stream_lines(client, _load_fixture("agent_turn_stream_with_tools.json"))
        # Only canonical text messages are compiled upstream; the tools[]
        # declaration never reaches the backend prompt.
        assert backend.turn_calls[0].prompt == (
            "[system]\n"
            "You are Qwen Code, an interactive CLI agent developed by "
            "Alibaba Group, specializing in software engineering tasks. "
            "Your primary goal is to help users safely and efficiently, "
            "adhering strictly to the following instructions and utilizing "
            "your available tools.\n\n"
            "[user]\nWhat files are in the current directory?"
        )

    def test_same_body_non_streamed_returns_json(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn(AGENT_TURN)])
        client = _client(_settings(), backend)
        payload = _load_fixture("agent_turn_stream_with_tools.json")
        payload["stream"] = False
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["finish_reason"] == "stop"
        message = body["choices"][0]["message"]
        assert message == {"role": "assistant", "content": AGENT_TURN}

    def test_tool_choice_required_is_tolerated(self) -> None:
        # Qwen Code only ever sends tool_choice 'required' or 'none'
        # (never 'auto'); both are accepted and ignored until M6.
        backend = FakeBackend(turns=[fake_text_turn(AGENT_TURN)])
        client = _client(_settings(), backend)
        payload = _load_fixture("agent_turn_stream_with_tools.json")
        payload["stream"] = False
        payload["tool_choice"] = "required"
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == AGENT_TURN


class TestPlainChatNonStream:
    """Fixture: plain_chat_non_stream.json (side-query shape)."""

    def test_returns_openai_non_stream_shape(self) -> None:
        answer = "It means something tried to call .map on undefined."
        backend = FakeBackend(turns=[fake_text_turn(answer)])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_load_fixture("plain_chat_non_stream.json"),
            headers=AUTH,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "deepseek-web"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["choices"][0]["message"]["content"] == answer


class TestToolHistoryTurn:
    """Fixture: tool_history_turn.json (the M6-pending loop shape)."""

    def test_is_deterministically_400_until_m6(self) -> None:
        # The shape is fixtured NOW so M6 has a pinned target; until tool
        # history can be compiled the gateway answers a deterministic 400.
        # This cannot happen in an M5 plain-chat session anyway: the
        # gateway never emits tool_calls yet, so Qwen Code can never hold
        # tool history against it.
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions",
            json=_load_fixture("tool_history_turn.json"),
            headers=AUTH,
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "UNSUPPORTED_MESSAGE"
        assert "M6" in error["message"]


class TestNonStandardExtras:
    """Fixture: non_standard_extras.json (harmless extensions)."""

    def test_extras_are_accepted_and_ignored(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("Hello!")])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_load_fixture("non_standard_extras.json"),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello!"
