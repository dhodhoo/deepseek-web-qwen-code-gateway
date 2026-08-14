"""M5 tests: real OpenAI SDK against a real uvicorn gateway (offline).

The strongest available offline proxy for ROADMAP M5's exit criterion
("real Qwen Code can use the gateway for plain chat"): drive the gateway
through an actual OpenAI client library over real HTTP — same wire
protocol Qwen Code's pinned openai Node SDK speaks — instead of raw
hand-rolled requests. FakeBackend answers, so no credentials are needed.

If the ``openai`` package is unavailable the tests skip (it is a dev
extra; see pyproject.toml).
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from pydantic import SecretStr

from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.server import create_app

openai = pytest.importorskip("openai")

MODEL = "deepseek-web"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def gateway_base_url():
    """Start a real gateway (uvicorn, FakeBackend) on a free port."""
    backend = FakeBackend(turns=[fake_text_turn("ok") for _ in range(16)])
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


class TestProviderModelSelection:
    def test_models_list_advertises_the_alias(self, sdk_client) -> None:
        models = sdk_client.models.list()
        ids = [model.id for model in models.data]
        assert MODEL in ids


class TestPlainChatThroughRealSdk:
    def test_non_stream(self, sdk_client) -> None:
        response = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            stream=False,  # Qwen Code always sends stream explicitly
        )
        assert response.object == "chat.completion"
        assert response.model == MODEL
        choice = response.choices[0]
        assert choice.message.role == "assistant"
        assert choice.message.content == "ok"
        assert choice.finish_reason == "stop"

    def test_streaming_with_include_usage(self, sdk_client) -> None:
        stream = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        parts: list[str] = []
        finish_reason = None
        for chunk in stream:
            assert chunk.object == "chat.completion.chunk"
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                parts.append(delta.content)
            if chunk.choices[0].finish_reason is not None:
                finish_reason = chunk.choices[0].finish_reason
        assert "".join(parts) == "ok"
        assert finish_reason == "stop"


class TestToolsToleranceThroughRealSdk:
    def test_tools_are_accepted_and_ignored(self, sdk_client) -> None:
        response = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            stream=False,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                            },
                            "required": ["file_path"],
                        },
                    },
                }
            ],
        )
        message = response.choices[0].message
        assert message.content == "ok"
        assert message.tool_calls is None  # ignored until M6, never faked
        assert response.choices[0].finish_reason == "stop"

    def test_non_standard_extras_via_extra_body(self, sdk_client) -> None:
        response = sdk_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            stream=False,
            extra_body={
                "enable_thinking": False,
                "reasoning_effort": "low",
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        assert response.choices[0].message.content == "ok"
