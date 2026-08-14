"""M6 tests: prompt-emulated tool calling through the HTTP surface.

One emulated tool call end-to-end (ROADMAP M6 exit): incoming tools
become prompt instructions, one strictly parsed control envelope in the
model output becomes a standard OpenAI ``tool_calls`` response (both
response modes), invalid envelopes stay honest plain text, and tool
history round-trips through the canonical conversation store
(docs/TOOL_CALLING_PROTOCOL.md, ADR-023).
"""

from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.server import create_app
from app.tools import TOOL_CALL_END_SENTINEL, TOOL_CALL_START_SENTINEL

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Reads a file.",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
}

ENVELOPE_TEXT = (
    f"{TOOL_CALL_START_SENTINEL}\n"
    '{"name":"read_file","arguments":{"file_path": "src/main.py"}}\n'
    f"{TOOL_CALL_END_SENTINEL}"
)


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(settings: GatewaySettings, backend: FakeBackend) -> TestClient:
    return TestClient(create_app(settings, backend))


def _chat_body(**overrides) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Read src/main.py"}],
    }
    body.update(overrides)
    return body


def _envelope_turn(prefix: str = "") -> list:
    return [
        MessageStarted(),
        TextDelta(prefix + ENVELOPE_TEXT),
        MessageFinished("stop"),
    ]


def _stream_chunks(client: TestClient, payload: dict) -> list[dict]:
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=AUTH
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line.strip()]
    assert lines[-1] == "data: [DONE]"
    chunks = []
    for line in lines[:-1]:
        assert line.startswith("data: ")
        chunks.append(json.loads(line[len("data: ") :]))
    return chunks


class TestNonStreamToolCallResponse:
    def test_envelope_becomes_structured_tool_calls(self) -> None:
        backend = FakeBackend(turns=[_envelope_turn()])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        )
        assert response.status_code == 200
        body = response.json()
        choice = body["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        message = choice["message"]
        assert message["role"] == "assistant"
        # Tool-calls-only turn: content is omitted (the client reads null).
        assert "content" not in message
        (tool_call,) = message["tool_calls"]
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", tool_call["id"])
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "read_file"
        # Arguments leave as the canonical compact JSON STRING.
        assert tool_call["function"]["arguments"] == (
            '{"file_path":"src/main.py"}'
        )

    def test_text_before_the_envelope_is_kept_as_content(self) -> None:
        backend = FakeBackend(
            turns=[_envelope_turn(prefix="I'll read that file. ")]
        )
        client = _client(_settings(), backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        message = body["choices"][0]["message"]
        assert message["content"] == "I'll read that file. "
        assert len(message["tool_calls"]) == 1
        assert body["choices"][0]["finish_reason"] == "tool_calls"

    def test_plain_answer_with_tools_has_no_tool_calls_key(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("plain answer")])
        client = _client(_settings(), backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        message = body["choices"][0]["message"]
        assert message == {"role": "assistant", "content": "plain answer"}
        assert body["choices"][0]["finish_reason"] == "stop"

    def test_unknown_tool_envelope_stays_honest_text(self) -> None:
        # The gateway never fabricates tool calls: an envelope naming a
        # tool that was not supplied renders as the model's plain text.
        unknown = ENVELOPE_TEXT.replace("read_file", "never_offered")
        backend = FakeBackend(
            turns=[[MessageStarted(), TextDelta(unknown), MessageFinished("stop")]]
        )
        client = _client(_settings(), backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        choice = body["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == unknown
        assert "tool_calls" not in choice["message"]


class TestStreamingToolCallResponse:
    def test_tool_call_chunks_and_finish_reason(self) -> None:
        backend = FakeBackend(turns=[_envelope_turn()])
        client = _client(_settings(), backend)
        chunks = _stream_chunks(
            client, _chat_body(stream=True, tools=[READ_FILE_TOOL])
        )
        assert {chunk["object"] for chunk in chunks} == {
            "chat.completion.chunk"
        }
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        # No envelope fragment may ever reach the wire.
        streamed_text = "".join(delta.get("content", "") for delta in deltas)
        assert TOOL_CALL_START_SENTINEL not in streamed_text
        assert "DSQG_TOOL_CALL" not in streamed_text

        tool_call_deltas = [delta for delta in deltas if "tool_calls" in delta]
        assert len(tool_call_deltas) == 2
        opener = tool_call_deltas[0]["tool_calls"][0]
        assert opener["index"] == 0
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", opener["id"])
        assert opener["type"] == "function"
        assert opener["function"]["name"] == "read_file"
        assert opener["function"]["arguments"] == ""
        assert tool_call_deltas[1]["tool_calls"][0]["function"][
            "arguments"
        ] == '{"file_path":"src/main.py"}'
        # The role rides the FIRST rendered chunk (M3 rule), exactly once;
        # the tool_calls opener follows without repeating it.
        assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
        assert sum("role" in delta for delta in deltas) == 1
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert chunks[-1]["choices"][0]["delta"] == {}

    def test_envelope_split_across_backend_deltas(self) -> None:
        # The backend "streams" the envelope one character cluster at a
        # time; the client-visible stream still renders exactly one clean
        # tool call and zero envelope fragments.
        text = "ok " + ENVELOPE_TEXT
        events = [MessageStarted()]
        for i in range(0, len(text), 3):
            events.append(TextDelta(text[i : i + 3]))
        events.append(MessageFinished("stop"))
        backend = FakeBackend(turns=[events])
        client = _client(_settings(), backend)
        chunks = _stream_chunks(
            client, _chat_body(stream=True, tools=[READ_FILE_TOOL])
        )
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        streamed_text = "".join(delta.get("content", "") for delta in deltas)
        assert streamed_text == "ok "
        tool_call_deltas = [delta for delta in deltas if "tool_calls" in delta]
        assert len(tool_call_deltas) == 2
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_plain_stream_with_tools_is_unchanged(self) -> None:
        backend = FakeBackend(
            turns=[
                [
                    MessageStarted(),
                    TextDelta("Hello "),
                    TextDelta("world"),
                    MessageFinished("stop"),
                ]
            ]
        )
        client = _client(_settings(), backend)
        chunks = _stream_chunks(
            client, _chat_body(stream=True, tools=[READ_FILE_TOOL])
        )
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        assert (
            "".join(delta.get("content", "") for delta in deltas)
            == "Hello world"
        )
        assert all("tool_calls" not in delta for delta in deltas)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


class TestToolInstructionsInPrompt:
    def test_tools_are_compiled_into_instructions(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        )
        prompt = backend.turn_calls[0].prompt
        assert prompt.startswith("[user]\nRead src/main.py\n\n[available tools]")
        assert "- read_file: Reads a file." in prompt
        assert TOOL_CALL_START_SENTINEL in prompt
        assert TOOL_CALL_END_SENTINEL in prompt

    def test_tool_choice_required_adds_the_must_instruction(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL], tool_choice="required"),
            headers=AUTH,
        )
        assert (
            "You MUST request exactly one tool call now"
            in backend.turn_calls[0].prompt
        )

    def test_tool_choice_none_disables_tools_entirely(self) -> None:
        # No instructions, no parsing: an envelope in the output streams
        # through as the plain text the model produced.
        backend = FakeBackend(
            turns=[[MessageStarted(), TextDelta(ENVELOPE_TEXT), MessageFinished("stop")]]
        )
        client = _client(_settings(), backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL], tool_choice="none"),
            headers=AUTH,
        ).json()
        assert "[available tools]" not in backend.turn_calls[0].prompt
        message = body["choices"][0]["message"]
        assert message["content"] == ENVELOPE_TEXT
        assert "tool_calls" not in message

    def test_malformed_tool_entries_are_skipped_not_rejected(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(
                tools=[
                    {"type": "banana", "function": {"name": "x"}},
                    READ_FILE_TOOL,
                    {"type": "function", "function": {"name": "bad name!"}},
                ]
            ),
            headers=AUTH,
        )
        assert response.status_code == 200
        prompt = backend.turn_calls[0].prompt
        assert "- read_file: Reads a file." in prompt
        assert "banana" not in prompt

    def test_only_invalid_tools_means_no_tool_instructions(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[{"type": "banana"}]),
            headers=AUTH,
        )
        assert backend.turn_calls[0].prompt == "[user]\nRead src/main.py"


class TestToolHistoryRoundTrip:
    """The M6 exit sequence: the gateway's tool call comes back as
    history and the conversation continues through the canonical store."""

    def _first_turn(self, backend: FakeBackend, client: TestClient) -> dict:
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        return body["choices"][0]["message"]["tool_calls"][0]

    def test_second_request_continues_the_same_conversation(self) -> None:
        backend = FakeBackend(
            turns=[
                _envelope_turn(),
                [
                    MessageStarted(),
                    TextDelta("The file prints hello."),
                    MessageFinished("stop"),
                ],
            ]
        )
        client = _client(_settings(), backend)

        tool_call = self._first_turn(backend, client)

        # Qwen Code re-sends the assistant tool call (content: null) plus
        # the tool result; the canonical store must recognize the prefix.
        follow_up = _chat_body(
            messages=[
                {"role": "user", "content": "Read src/main.py"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": 'print("hello")',
                },
                {"role": "user", "content": "What does it do?"},
            ],
            tools=[READ_FILE_TOOL],
        )
        response = client.post(
            "/v1/chat/completions", json=follow_up, headers=AUTH
        )
        assert response.status_code == 200
        assert (
            response.json()["choices"][0]["message"]["content"]
            == "The file prints hello."
        )

        # Session reuse: one backend session, and the second prompt is
        # only the DELTA (tool result + new user message) + instructions.
        assert len(backend.sessions_created) == 1
        prompt = backend.turn_calls[1].prompt
        assert "[user]\nRead src/main.py" not in prompt
        assert prompt.startswith(
            "[tool result]\n"
            f"id: {tool_call['id']}\n"
            "tool: read_file\n"
            "result:\n"
            'print("hello")\n'
            "[end tool result]"
        )
        assert "[available tools]" in prompt
        assert "[user]\nWhat does it do?" in prompt

    def test_arguments_round_trip_through_normalization(self) -> None:
        # The client re-sends arguments with DIFFERENT whitespace; the
        # canonical store must still match (normalized equality, ADR-023).
        backend = FakeBackend(
            turns=[
                _envelope_turn(),
                fake_text_turn("done"),
            ]
        )
        client = _client(_settings(), backend)
        tool_call = self._first_turn(backend, client)
        re_sent = dict(tool_call)
        re_sent["function"] = {
            "name": tool_call["function"]["name"],
            # Pretty-printed: would break naive string comparison.
            "arguments": json.dumps(
                json.loads(tool_call["function"]["arguments"]), indent=2
            ),
        }
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(
                messages=[
                    {"role": "user", "content": "Read src/main.py"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [re_sent],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": "x",
                    },
                ],
                tools=[READ_FILE_TOOL],
            ),
            headers=AUTH,
        )
        assert response.status_code == 200
        # Prefix matched → same session, delta-only prompt.
        assert len(backend.sessions_created) == 1
        assert "[user]\nRead src/main.py" not in backend.turn_calls[1].prompt


class TestToolShapedRejections:
    def _post(self, backend: FakeBackend, payload: dict):
        return _client(_settings(), backend).post(
            "/v1/chat/completions", json=payload, headers=AUTH
        )

    def test_tool_message_without_tool_call_id_is_400(self) -> None:
        response = self._post(
            FakeBackend(),
            _chat_body(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "tool", "content": "result"},
                ]
            ),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_MESSAGE"

    def test_assistant_tool_call_with_bad_arguments_is_400(self) -> None:
        response = self._post(
            FakeBackend(),
            _chat_body(
                messages=[
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "run",
                                    "arguments": "{broken",
                                },
                            }
                        ],
                    },
                ]
            ),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_MESSAGE"

    def test_assistant_null_content_without_tool_calls_is_400(self) -> None:
        response = self._post(
            FakeBackend(),
            _chat_body(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": None},
                ]
            ),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "UNSUPPORTED_MESSAGE"
