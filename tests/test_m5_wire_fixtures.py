"""M5 tests: Qwen Code wire fixtures drive the gateway surface (offline).

The fixture bodies live in ``tests/fixtures/qwen_code_wire/`` and are
synthesized from the source-verified Qwen Code wire facts in
docs/UPSTREAM_NOTES.md (see the fixture README for provenance). These
tests pin, per ROADMAP M5, that the exact current agent request/history
format is covered:

* agent turn with tools[] + stream_options     → plain-text 200 (since
  M6 the tools are compiled into prompt instructions, but the scripted
  answer is plain text, so no tool_calls deltas appear);
* side query (stream: false, no tools)         → 200 non-stream JSON;
* structured side query (stream: false,
  tool_choice 'required' + single
  respond_in_schema tool; traffic-verified
  2026-08-14)                                  → 200 non-stream plain text;
* tool-loop history (assistant tool_calls /
  role=tool)                                   → 200, compiled into the
  prompt (M6, ADR-023);
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
        # The scripted answer contains no control envelope, so even with
        # tools enabled (M6) the stream carries no tool_calls deltas — and
        # no usage chunk. Qwen Code tolerates the missing usage chunk
        # (docs/UPSTREAM_NOTES.md, M5 verification).
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        assert all("tool_calls" not in delta for delta in deltas)
        assert all("usage" not in chunk for chunk in chunks)

    def test_backend_receives_compiled_prompt_plus_tool_instructions(self) -> None:
        backend = FakeBackend(turns=[AGENT_SSE_TURN])
        client = _client(_settings(), backend)
        _stream_lines(client, _load_fixture("agent_turn_stream_with_tools.json"))
        # M6 (ADR-023, superseding the M5 "tools never reach the backend"
        # invariant): the prompt is exactly the compiled canonical
        # messages followed by ONE deterministic tool-control block.
        prompt = backend.turn_calls[0].prompt
        assert prompt.startswith(
            "[system]\n"
            "You are Qwen Code, an interactive CLI agent developed by "
            "Alibaba Group, specializing in software engineering tasks. "
            "Your primary goal is to help users safely and efficiently, "
            "adhering strictly to the following instructions and utilizing "
            "your available tools.\n\n"
            "[user]\nWhat files are in the current directory?\n\n"
        )
        instructions = prompt.split("[user]\nWhat files are in the current directory?\n\n", 1)[1]
        assert instructions.startswith("[available tools]")
        assert instructions.count("[available tools]") == 1
        # The tools arrive as prompt instructions, never as raw JSON noise:
        # each tool renders as a "- name: description" line.
        assert "- read_file:" in instructions
        assert "- run_shell_command:" in instructions
        assert "<<<DSQG_TOOL_CALL>>>" in instructions
        assert "<<<DSQG_END_TOOL_CALL>>>" in instructions

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
        # (never 'auto'). Since M6 'required' adds the MUST-envelope
        # instruction to the prompt; since M7 (ADR-028) a missing
        # envelope additionally triggers ONE bounded repair retry. The
        # scripted model ignores both and answers plain text anyway,
        # which the client tolerates (observed live in M5).
        backend = FakeBackend(
            turns=[fake_text_turn(AGENT_TURN), fake_text_turn(AGENT_TURN)]
        )
        client = _client(_settings(), backend)
        payload = _load_fixture("agent_turn_stream_with_tools.json")
        payload["stream"] = False
        payload["tool_choice"] = "required"
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == AGENT_TURN
        # Bounded repair: exactly two backend calls; the retry prompt
        # carries the static repair hint (never echoed model output).
        assert len(backend.turn_calls) == 2
        assert (
            "did not use the required tool-call control format"
            in backend.turn_calls[1].prompt
        )


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


class TestSideQueryRespondInSchema:
    """Fixture: side_query_respond_in_schema.json.

    Traffic-verified shape (2026-08-14): alongside each user submission
    Qwen Code fires a non-streaming side query with ``tool_choice:
    'required'`` and a single ``respond_in_schema`` tool. Since M6 the
    gateway compiles that tool into a MUST-envelope instruction; since
    M7 (ADR-028) a missing envelope triggers one bounded repair retry.
    The scripted model ignores both and keeps answering plain text, and
    the client tolerates it (observed live: the session continued
    normally).
    """

    def test_plain_text_200_despite_required_single_tool(self) -> None:
        answer = "Listing directory contents"
        backend = FakeBackend(
            turns=[fake_text_turn(answer), fake_text_turn(answer)]
        )
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_load_fixture("side_query_respond_in_schema.json"),
            headers=AUTH,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["choices"][0]["message"] == {
            "role": "assistant",
            "content": answer,
        }
        # One bounded repair retry happened before the honest fallback.
        assert len(backend.turn_calls) == 2


class TestToolHistoryTurn:
    """Fixture: tool_history_turn.json (the tool-loop history shape).

    M6 (ADR-023): the pinned 400 is gone — assistant ``tool_calls`` and
    ``role=tool`` now compile into deterministic prompt blocks, so a
    real tool-loop continuation against the gateway is representable.
    """

    def test_tool_history_compiles_and_streams_200(self) -> None:
        follow_up = "This file defines and runs a minimal main()."
        backend = FakeBackend(
            turns=[
                [
                    MessageStarted(),
                    TextDelta(follow_up),
                    MessageFinished("stop"),
                ]
            ]
        )
        client = _client(_settings(), backend)
        lines = _stream_lines(
            client, _load_fixture("tool_history_turn.json")
        )
        assert lines[-1] == "data: [DONE]"
        chunks = [_parse(line) for line in lines[:-1]]
        full_text = "".join(
            chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
        )
        assert full_text == follow_up
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

        prompt = backend.turn_calls[0].prompt
        # The assistant tool call renders with its id and normalized
        # arguments; the result renders as DATA under [tool result].
        assert (
            "[assistant tool call]\n"
            "id: call_9f1d2c3b4a5e6f70\n"
            "tool: read_file\n"
            'arguments: {"file_path":"src/main.py"}' in prompt
        )
        assert (
            "[tool result]\n"
            "id: call_9f1d2c3b4a5e6f70\n"
            "tool: read_file\n"
            "result:\n"
            'def main():\n    print("hello")' in prompt
        )
        assert "[end tool result]" in prompt
        # Tool names resolve through the assistant call seen earlier —
        # not through the result's own (absent) name field.
        assert "tool: unknown" not in prompt
        # Tool instructions for THIS request's tools come last, after the
        # compiled history blocks.
        assert prompt.rindex("[available tools]") > prompt.rindex(
            "[end tool result]"
        )
        assert "- read_file: Reads and returns the content of a specified file." in prompt


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
