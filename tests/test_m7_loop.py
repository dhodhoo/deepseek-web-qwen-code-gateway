"""M7 tests: the multi-turn tool loop (ROADMAP M7 exit, ADR-028).

Covers the five M7 build items offline:

* persistent tool-call ID mapping — ``tool_call_index`` over canonical
  history (first occurrence wins, deterministic);
* repeated tool-result/model cycles — three sequential tool
  interactions plus a final answer through the real app with a
  scripted FakeBackend (the ROADMAP exit, minus the live client);
* streaming tool-control buffering — tool-enabled turns are fully
  buffered before any SSE byte; pre-response failures answer with an
  HTTP status; chunk shapes stay identical to M6;
* bounded repair policy — one retry when a ``required`` turn yields no
  envelope or when an envelope is malformed/truncated; the retry prompt
  carries a STATIC hint; the fallback after exhaustion is honest text;
  multi-attempt turns invalidate the backend link after committing;
* history validation — lenient (ADR-023): orphan tool results compile
  as-is (tool name ``unknown``, id verbatim) and findings are logged,
  never rejected.

The gateway never executes tools: every "tool execution" in these tests
is the test playing Qwen Code, and the FakeBackend only ever produces
the next scripted INFERENCE.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import (
    BackendMessageId,
    MessageFinished,
    MessageStarted,
    TextDelta,
)
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.conversation import (
    CanonicalMessage,
    CanonicalToolCall,
    tool_call_index,
    validate_tool_history,
)
from app.server import create_app
from app.tool_envelope import EnvelopeParser
from app.tools import (
    TOOL_CALL_END_SENTINEL,
    TOOL_CALL_START_SENTINEL,
    CanonicalTool,
)

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

#: First line of the static repair hint (server._tool_repair_hint).
REPAIR_HINT_MARKER = "did not use the required tool-call control format"

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

LIST_DIR_TOOL = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "Lists a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def _settings() -> GatewaySettings:
    return GatewaySettings(
        backend_type="fake", gateway_api_key=SecretStr("test-key")
    )


def _client(backend: FakeBackend) -> TestClient:
    return TestClient(create_app(_settings(), backend))


def _chat_body(**overrides) -> dict:
    body: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Read src/main.py"}],
    }
    body.update(overrides)
    return body


def _envelope_text(
    name: str = "read_file",
    arguments: str = '{"file_path":"src/main.py"}',
) -> str:
    return (
        f"{TOOL_CALL_START_SENTINEL}\n"
        f'{{"name":"{name}","arguments":{arguments}}}\n'
        f"{TOOL_CALL_END_SENTINEL}"
    )


def _envelope_turn(
    name: str = "read_file",
    arguments: str = '{"file_path":"src/main.py"}',
) -> list:
    return [
        MessageStarted(),
        TextDelta(_envelope_text(name, arguments)),
        MessageFinished("stop"),
    ]


def _stream_chunks(client: TestClient, payload: dict) -> list[dict]:
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=AUTH
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line.strip()]
    assert lines[-1] == "data: [DONE]"
    return [json.loads(line[len("data: ") :]) for line in lines[:-1]]


def _read_tool() -> CanonicalTool:
    return CanonicalTool(name="read_file", description="", schema=None)


# ---------------------------------------------------------------------------
# Persistent tool-call ID index (conversation.py)
# ---------------------------------------------------------------------------


def _assistant(call_id: str, name: str = "read_file") -> CanonicalMessage:
    return CanonicalMessage(
        role="assistant",
        content=None,
        tool_calls=(
            CanonicalToolCall(id=call_id, function_name=name, arguments_json="{}"),
        ),
    )


def _tool_result(call_id: str | None, content: str = "ok") -> CanonicalMessage:
    return CanonicalMessage(role="tool", content=content, tool_call_id=call_id)


class TestToolCallIndex:
    def test_maps_every_assistant_tool_call(self) -> None:
        history = [
            CanonicalMessage(role="user", content="go"),
            _assistant("call_1", "read_file"),
            _tool_result("call_1"),
            _assistant("call_2", "list_directory"),
        ]
        index = tool_call_index(history)
        assert set(index) == {"call_1", "call_2"}
        assert index["call_1"].function_name == "read_file"
        assert index["call_2"].function_name == "list_directory"

    def test_first_occurrence_wins(self) -> None:
        history = [_assistant("call_dup", "read_file"), _assistant("call_dup", "evil")]
        assert tool_call_index(history)["call_dup"].function_name == "read_file"

    def test_empty_history(self) -> None:
        assert tool_call_index([]) == {}


class TestValidateToolHistory:
    def test_well_formed_loop_is_clean(self) -> None:
        history = [
            CanonicalMessage(role="user", content="go"),
            _assistant("call_1"),
            _tool_result("call_1"),
        ]
        assert validate_tool_history(history).clean

    def test_pending_tool_call_without_result_is_clean(self) -> None:
        # Mid-loop state: the result simply has not arrived yet.
        history = [CanonicalMessage(role="user", content="go"), _assistant("call_1")]
        assert validate_tool_history(history).clean

    def test_orphan_tool_result_is_reported(self) -> None:
        history = [
            CanonicalMessage(role="user", content="go"),
            _assistant("call_1"),
            _tool_result("call_NEVER_ISSUED"),
        ]
        findings = validate_tool_history(history)
        assert not findings.clean
        assert findings.orphan_tool_results == ("call_NEVER_ISSUED",)
        assert findings.missing_tool_call_ids == 0

    def test_missing_tool_call_id_is_reported(self) -> None:
        history = [
            CanonicalMessage(role="user", content="go"),
            _tool_result(None),
            _tool_result(""),
        ]
        findings = validate_tool_history(history)
        assert findings.missing_tool_call_ids == 2
        assert findings.orphan_tool_results == ()


# ---------------------------------------------------------------------------
# Envelope parser: invalid_envelope_seen flag (tool_envelope.py)
# ---------------------------------------------------------------------------


class TestInvalidEnvelopeFlag:
    def test_valid_envelope_does_not_set_the_flag(self) -> None:
        parser = EnvelopeParser([_read_tool()])
        parser.feed(_envelope_text())
        parser.finalize()
        assert parser.emitted_call is not None
        assert parser.invalid_envelope_seen is False

    def test_plain_text_does_not_set_the_flag(self) -> None:
        parser = EnvelopeParser([_read_tool()])
        parser.feed("just an answer, no envelope")
        parser.finalize()
        assert parser.invalid_envelope_seen is False

    def test_invalid_region_sets_the_flag(self) -> None:
        parser = EnvelopeParser([_read_tool()])
        parser.feed(
            f"{TOOL_CALL_START_SENTINEL}\n{{broken json}}\n{TOOL_CALL_END_SENTINEL}"
        )
        parser.finalize()
        assert parser.emitted_call is None
        assert parser.invalid_envelope_seen is True

    def test_unknown_tool_name_sets_the_flag(self) -> None:
        parser = EnvelopeParser([_read_tool()])
        parser.feed(_envelope_text(name="never_offered"))
        parser.finalize()
        assert parser.emitted_call is None
        assert parser.invalid_envelope_seen is True

    def test_truncated_envelope_sets_the_flag(self) -> None:
        parser = EnvelopeParser([_read_tool()])
        parser.feed(f'{TOOL_CALL_START_SENTINEL}\n{{"name":"read_file"')
        parser.finalize()
        assert parser.emitted_call is None
        assert parser.invalid_envelope_seen is True


# ---------------------------------------------------------------------------
# Bounded repair policy (server.py, non-streaming surface)
# ---------------------------------------------------------------------------


class TestBoundedRepairPolicy:
    def test_required_plain_then_envelope_succeeds_on_retry(self) -> None:
        # Turn 1: a valid envelope commits and threads parent "up-1".
        # Turn 2 (required): attempt 1 answers plain text → one bounded
        # repair retry → attempt 2 emits the envelope.
        first_envelope = [
            MessageStarted(),
            TextDelta(_envelope_text(arguments='{"file_path":"a.py"}')),
            BackendMessageId("up-1"),
            MessageFinished("stop"),
        ]
        backend = FakeBackend(
            turns=[
                first_envelope,
                fake_text_turn("I can just answer directly."),
                _envelope_turn(arguments='{"file_path":"b.py"}'),
                fake_text_turn("done"),
            ]
        )
        client = _client(backend)

        first = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        call_1 = first["choices"][0]["message"]["tool_calls"][0]

        second_messages = [
            {"role": "user", "content": "Read src/main.py"},
            {"role": "assistant", "content": None, "tool_calls": [call_1]},
            {"role": "tool", "tool_call_id": call_1["id"], "content": "a"},
            {"role": "user", "content": "Now read b.py"},
        ]
        second = client.post(
            "/v1/chat/completions",
            json=_chat_body(
                messages=second_messages,
                tools=[READ_FILE_TOOL],
                tool_choice="required",
            ),
            headers=AUTH,
        ).json()
        assert second["choices"][0]["finish_reason"] == "tool_calls"
        call_2 = second["choices"][0]["message"]["tool_calls"][0]
        assert call_2["function"]["name"] == "read_file"
        assert call_2["function"]["arguments"] == '{"file_path":"b.py"}'

        # Exactly one repair retry: two backend calls for turn 2, both on
        # the SAME session and the SAME ORIGINAL parent (re-branching —
        # the failed attempt never threads upstream, ADR-028 points 2–3).
        assert len(backend.turn_calls) == 3
        attempt_1, attempt_2 = backend.turn_calls[1], backend.turn_calls[2]
        assert attempt_1.session_id == attempt_2.session_id == "fake-session-1"
        assert attempt_1.parent_message_id == "up-1"
        assert attempt_2.parent_message_id == "up-1"
        # The retry prompt = original prompt + the STATIC repair hint.
        assert REPAIR_HINT_MARKER not in attempt_1.prompt
        assert attempt_2.prompt.startswith(attempt_1.prompt)
        assert REPAIR_HINT_MARKER in attempt_2.prompt
        assert "read_file" in attempt_2.prompt

        # Multi-attempt turn → backend link invalidated AFTER the commit:
        # the follow-up rebuilds from canonical history (new session,
        # full-history prompt), and the repaired tool call survives.
        follow_up = client.post(
            "/v1/chat/completions",
            json=_chat_body(
                messages=second_messages
                + [
                    {"role": "assistant", "content": None, "tool_calls": [call_2]},
                    {"role": "tool", "tool_call_id": call_2["id"], "content": "b"},
                    {"role": "user", "content": "thanks"},
                ],
                tools=[READ_FILE_TOOL],
            ),
            headers=AUTH,
        )
        assert follow_up.status_code == 200
        assert len(backend.sessions_created) == 2
        rebuild_prompt = backend.turn_calls[3].prompt
        assert "[user]\nRead src/main.py" in rebuild_prompt
        assert "[user]\nNow read b.py" in rebuild_prompt

    def test_required_plain_twice_is_bounded_and_honest(self) -> None:
        backend = FakeBackend(
            turns=[
                fake_text_turn("first plain answer"),
                fake_text_turn("second plain answer"),
                fake_text_turn("never reached"),
            ]
        )
        client = _client(backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL], tool_choice="required"),
            headers=AUTH,
        ).json()
        choice = body["choices"][0]
        # Bounded: exactly two attempts, then the HONEST text of the last
        # attempt (never a fabricated tool call).
        assert len(backend.turn_calls) == 2
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == "second plain answer"
        assert "tool_calls" not in choice["message"]

    def test_optional_malformed_envelope_triggers_repair(self) -> None:
        malformed = (
            f"{TOOL_CALL_START_SENTINEL}\n"
            '{"name":"read_file","arguments":{"file_path": \n'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        backend = FakeBackend(
            turns=[
                [MessageStarted(), TextDelta(malformed), MessageFinished("stop")],
                _envelope_turn(),
            ]
        )
        client = _client(backend)
        # No tool_choice: a plain answer would NOT retry, but a malformed
        # envelope proves the model tried the format → one repair retry.
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert len(backend.turn_calls) == 2
        assert REPAIR_HINT_MARKER in backend.turn_calls[1].prompt

    def test_optional_truncated_envelope_triggers_repair(self) -> None:
        truncated = f'{TOOL_CALL_START_SENTINEL}\n{{"name":"read_file"'
        backend = FakeBackend(
            turns=[
                [MessageStarted(), TextDelta(truncated), MessageFinished("stop")],
                _envelope_turn(),
            ]
        )
        client = _client(backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert len(backend.turn_calls) == 2

    def test_optional_plain_text_does_not_repair(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("plain answer")])
        client = _client(backend)
        body = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        assert len(backend.turn_calls) == 1
        assert body["choices"][0]["message"]["content"] == "plain answer"

    def test_valid_envelope_first_attempt_keeps_the_link(self) -> None:
        backend = FakeBackend(
            turns=[_envelope_turn(), fake_text_turn("The file prints hello.")]
        )
        client = _client(backend)
        first = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        call = first["choices"][0]["message"]["tool_calls"][0]
        assert len(backend.turn_calls) == 1  # no repair happened
        second = client.post(
            "/v1/chat/completions",
            json=_chat_body(
                messages=[
                    {"role": "user", "content": "Read src/main.py"},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": call["id"], "content": "x"},
                    {"role": "user", "content": "What does it do?"},
                ],
                tools=[READ_FILE_TOOL],
            ),
            headers=AUTH,
        )
        assert second.status_code == 200
        # Single-attempt turns keep the M6 behavior: same session, delta.
        assert len(backend.sessions_created) == 1
        assert "[user]\nRead src/main.py" not in backend.turn_calls[1].prompt

    def test_repair_failure_answers_with_http_status(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.RATE_LIMITED, message="slow down"
        )
        backend = FakeBackend(turns=[[failure]])
        client = _client(backend)
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL], tool_choice="required"),
            headers=AUTH,
        )
        # Buffered tool turn: the failure is pre-response → HTTP status.
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Streaming tool-control buffering (server.py + unchanged sse_stream)
# ---------------------------------------------------------------------------


class TestStreamingBufferedToolTurns:
    def test_valid_envelope_chunk_shape_matches_m6(self) -> None:
        backend = FakeBackend(turns=[_envelope_turn()])
        client = _client(backend)
        chunks = _stream_chunks(
            client, _chat_body(stream=True, tools=[READ_FILE_TOOL])
        )
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        streamed_text = "".join(delta.get("content", "") for delta in deltas)
        assert streamed_text == ""
        assert TOOL_CALL_START_SENTINEL not in streamed_text
        tool_call_deltas = [delta for delta in deltas if "tool_calls" in delta]
        assert len(tool_call_deltas) == 2
        opener = tool_call_deltas[0]["tool_calls"][0]
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", opener["id"])
        assert opener["type"] == "function"
        assert opener["function"]["name"] == "read_file"
        assert opener["function"]["arguments"] == ""
        assert tool_call_deltas[1]["tool_calls"][0]["function"][
            "arguments"
        ] == '{"file_path":"src/main.py"}'
        assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
        assert sum("role" in delta for delta in deltas) == 1
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_repair_stream_emits_only_the_final_outcome(self) -> None:
        # Attempt 1's plain text must never reach the wire: the buffered
        # path decides BEFORE the first SSE byte.
        backend = FakeBackend(
            turns=[fake_text_turn("Let me think about it plainly."), _envelope_turn()]
        )
        client = _client(backend)
        chunks = _stream_chunks(
            client,
            _chat_body(stream=True, tools=[READ_FILE_TOOL], tool_choice="required"),
        )
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        streamed_text = "".join(delta.get("content", "") for delta in deltas)
        assert streamed_text == ""
        assert "Let me think" not in json.dumps(chunks)
        tool_call_deltas = [delta for delta in deltas if "tool_calls" in delta]
        assert len(tool_call_deltas) == 2
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert len(backend.turn_calls) == 2

    def test_repair_exhaustion_streams_honest_text(self) -> None:
        backend = FakeBackend(
            turns=[fake_text_turn("first plain"), fake_text_turn("second plain")]
        )
        client = _client(backend)
        chunks = _stream_chunks(
            client,
            _chat_body(stream=True, tools=[READ_FILE_TOOL], tool_choice="required"),
        )
        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        assert (
            "".join(delta.get("content", "") for delta in deltas)
            == "second plain"
        )
        assert all("tool_calls" not in delta for delta in deltas)
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    def test_streaming_tool_failure_answers_with_http_status(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_5XX, message="boom"
        )
        backend = FakeBackend(turns=[[TextDelta("partial"), failure]])
        client = _client(backend)
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_chat_body(stream=True, tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ) as response:
            # No HTTP 200 + in-stream error envelope: the buffered tool
            # path fails PRE-response (ADR-028 point 1).
            assert response.status_code == 502


# ---------------------------------------------------------------------------
# Repeated tool-result/model cycles (ROADMAP M7 exit, offline form)
# ---------------------------------------------------------------------------


class TestMultiCycleLoop:
    def test_three_sequential_tool_interactions_then_final_answer(self) -> None:
        backend = FakeBackend(
            turns=[
                _envelope_turn("list_directory", '{"path":"."}'),
                _envelope_turn("read_file", '{"file_path":"src/main.py"}'),
                _envelope_turn("read_file", '{"file_path":"tests/test_main.py"}'),
                [
                    MessageStarted(),
                    TextDelta("The bug was an off-by-one in the loop bound."),
                    MessageFinished("stop"),
                ],
            ]
        )
        client = _client(backend)
        tools = [LIST_DIR_TOOL, READ_FILE_TOOL]

        messages: list[dict] = [
            {"role": "user", "content": "Find and fix the bug."}
        ]
        ids: list[str] = []
        expected_tool_names = ["list_directory", "read_file", "read_file"]
        for cycle in range(3):
            response = client.post(
                "/v1/chat/completions",
                json=_chat_body(
                    messages=messages, tools=tools, tool_choice="required"
                ),
                headers=AUTH,
            )
            assert response.status_code == 200
            choice = response.json()["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            call = choice["message"]["tool_calls"][0]
            assert call["function"]["name"] == expected_tool_names[cycle]
            assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", call["id"])
            ids.append(call["id"])
            # The CLIENT (Qwen Code in production) executes the tool and
            # re-sends the history; the gateway only does inference.
            messages = messages + [
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": f"result-{cycle + 1}",
                },
            ]

        final = client.post(
            "/v1/chat/completions",
            json=_chat_body(messages=messages, tools=tools),
            headers=AUTH,
        ).json()
        assert final["choices"][0]["finish_reason"] == "stop"
        assert (
            final["choices"][0]["message"]["content"]
            == "The bug was an off-by-one in the loop bound."
        )

        # Four inferences, one session: no repair happened, so the M4/M6
        # delta reuse stayed intact across the whole loop.
        assert len(backend.turn_calls) == 4
        assert len(backend.sessions_created) == 1
        # Every delta prompt resolved its tool result to the RIGHT tool
        # name through the persistent ID index (never "unknown"), with
        # the id preserved verbatim.
        for cycle in range(3):
            prompt = backend.turn_calls[cycle + 1].prompt
            assert f"id: {ids[cycle]}" in prompt
            assert f"tool: {expected_tool_names[cycle]}" in prompt
            assert f"result-{cycle + 1}" in prompt
        assert all("tool: unknown" not in c.prompt for c in backend.turn_calls)

        # Canonical state (the truth) holds the full loop:
        # user + (assistant tool call + tool result) x3 + final assistant.
        conversations = client.app.state.store.conversations()
        assert len(conversations) == 1
        history = conversations[0].messages
        assert [m.role for m in history] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]
        tool_message_ids = [m.tool_call_id for m in history if m.role == "tool"]
        assert tool_message_ids == ids
        assert history[-1].content == "The bug was an off-by-one in the loop bound."


# ---------------------------------------------------------------------------
# Lenient history validation through the HTTP surface
# ---------------------------------------------------------------------------


class TestLenientHistoryValidation:
    def test_orphan_tool_result_compiles_as_unknown_and_is_logged(
        self, caplog
    ) -> None:
        backend = FakeBackend(turns=[fake_text_turn("done")])
        client = _client(backend)
        messages = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_A",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"file_path":"a.py"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_NEVER_ISSUED",
                "content": "stale result",
            },
        ]
        with caplog.at_level(logging.WARNING, logger="dsqg.server"):
            response = client.post(
                "/v1/chat/completions",
                json=_chat_body(messages=messages, tools=[READ_FILE_TOOL]),
                headers=AUTH,
            )
        # Lenient (ADR-023): compiled as-is, never rejected.
        assert response.status_code == 200
        prompt = backend.turn_calls[0].prompt
        assert "id: call_NEVER_ISSUED" in prompt
        assert "tool: unknown" in prompt
        assert "stale result" in prompt
        # Operators get a minimal warning (counts/ids, never content).
        assert any(
            "tool history anomalies" in record.message
            for record in caplog.records
        )

    def test_clean_tool_history_logs_nothing(self, caplog) -> None:
        backend = FakeBackend(turns=[_envelope_turn(), fake_text_turn("done")])
        client = _client(backend)
        first = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[READ_FILE_TOOL]),
            headers=AUTH,
        ).json()
        call = first["choices"][0]["message"]["tool_calls"][0]
        with caplog.at_level(logging.WARNING, logger="dsqg.server"):
            response = client.post(
                "/v1/chat/completions",
                json=_chat_body(
                    messages=[
                        {"role": "user", "content": "Read src/main.py"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [call],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "x",
                        },
                    ],
                    tools=[READ_FILE_TOOL],
                ),
                headers=AUTH,
            )
        assert response.status_code == 200
        assert not any(
            "tool history anomalies" in record.message
            for record in caplog.records
        )
