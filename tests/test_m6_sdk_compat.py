"""M6 tests: one emulated tool call through the REAL OpenAI SDK (offline).

The strongest available offline proxy for ROADMAP M6's exit criterion
("real Qwen Code receives and executes one structured tool call"): drive
the gateway through an actual OpenAI client library over real HTTP and
verify the SDK — the same wire contract Qwen Code's pinned openai Node
SDK speaks — parses the gateway's ``tool_calls`` in BOTH response modes,
then feeds the tool result back as history. FakeBackend scripts the
envelope, so no credentials are needed.

If the ``openai`` package is unavailable the tests skip (it is a dev
extra; see pyproject.toml).
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from pydantic import SecretStr

from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.server import create_app
from app.tools import TOOL_CALL_END_SENTINEL, TOOL_CALL_START_SENTINEL

openai = pytest.importorskip("openai")

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
    '{"name":"read_file","arguments":{"file_path":"src/main.py"}}\n'
    f"{TOOL_CALL_END_SENTINEL}"
)


def _envelope_turn() -> list:
    return [MessageStarted(), TextDelta(ENVELOPE_TEXT), MessageFinished("stop")]


def _chunked_envelope_turn() -> list:
    events: list = [MessageStarted()]
    for i in range(0, len(ENVELOPE_TEXT), 4):
        events.append(TextDelta(ENVELOPE_TEXT[i : i + 4]))
    events.append(MessageFinished("stop"))
    return events


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def gateway_base_url():
    """Start a real gateway (uvicorn, FakeBackend) on a free port.

    Scripted turn order (consumed sequentially by the tests below):
    1. non-stream envelope, 2. streamed envelope, 3. envelope for the
    round-trip test's first call, 4. plain follow-up after the tool
    result comes back as history, 5. the same answer again — since
    ADR-035 every envelope-less tool-enabled turn pays ONE bounded
    repair retry, and the repeated answer flushes.
    """
    backend = FakeBackend(
        turns=[
            _envelope_turn(),
            _chunked_envelope_turn(),
            _envelope_turn(),
            fake_text_turn("It prints hello."),
            fake_text_turn("It prints hello."),
        ]
    )
    settings = GatewaySettings(
        backend_type="fake", gateway_api_key=SecretStr("test-key")
    )
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, backend),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=0.25) as probe:
                if probe.status == 200:
                    break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("gateway did not become healthy in time")

    yield f"{base}/v1"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def sdk_client(gateway_base_url):
    return openai.OpenAI(base_url=gateway_base_url, api_key="test-key")


class TestToolCallThroughRealSdk:
    def test_non_stream_tool_call_is_parsed_by_the_sdk(self, sdk_client) -> None:
        response = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Read src/main.py"}],
            stream=False,
            tools=[READ_FILE_TOOL],
        )
        choice = response.choices[0]
        assert choice.finish_reason == "tool_calls"
        message = choice.message
        assert message.role == "assistant"
        assert message.content in (None, "")
        assert message.tool_calls is not None
        (tool_call,) = message.tool_calls
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", tool_call.id)
        assert tool_call.type == "function"
        assert tool_call.function.name == "read_file"
        # The SDK hands arguments back as the JSON STRING we emitted.
        assert tool_call.function.arguments == '{"file_path":"src/main.py"}'

    def test_streamed_tool_call_is_parsed_by_the_sdk(self, sdk_client) -> None:
        stream = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Read src/main.py"}],
            stream=True,
            stream_options={"include_usage": True},
            tools=[READ_FILE_TOOL],
        )
        ids: set[str] = set()
        names: list[str] = []
        arguments_parts: list[str] = []
        finish_reason = None
        content_parts: list[str] = []
        for chunk in stream:
            assert chunk.object == "chat.completion.chunk"
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
            for tool_call in delta.tool_calls or []:
                if tool_call.id:
                    ids.add(tool_call.id)
                if tool_call.function and tool_call.function.name:
                    names.append(tool_call.function.name)
                if tool_call.function and tool_call.function.arguments:
                    arguments_parts.append(tool_call.function.arguments)
            if chunk.choices[0].finish_reason is not None:
                finish_reason = chunk.choices[0].finish_reason
        # No envelope fragment ever reached the client as content.
        assert "".join(content_parts) == ""
        assert len(ids) == 1
        (call_id,) = ids
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", call_id)
        assert names == ["read_file"]
        assert "".join(arguments_parts) == '{"file_path":"src/main.py"}'
        assert finish_reason == "tool_calls"

    def test_tool_result_round_trip_via_sdk_messages(self, sdk_client) -> None:
        # The full M6 exit sequence through the real SDK: the gateway
        # emits a structured tool call; the "client executes it" and
        # re-sends the assistant tool_calls message plus the role=tool
        # result; the conversation continues. Prefix resolution against
        # the canonical store must recognize the re-sent history.
        first = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Read src/main.py"}],
            stream=False,
            tools=[READ_FILE_TOOL],
        )
        tool_call = first.choices[0].message.tool_calls[0]

        follow_up = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "user", "content": "Read src/main.py"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": 'print("hello")',
                },
                {"role": "user", "content": "What does it do?"},
            ],
            stream=False,
            tools=[READ_FILE_TOOL],
        )
        choice = follow_up.choices[0]
        assert choice.finish_reason == "stop"
        assert choice.message.content == "It prints hello."
        assert choice.message.tool_calls is None
