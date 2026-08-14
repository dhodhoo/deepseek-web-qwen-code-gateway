"""M2 tests: HTTP surface via TestClient + FakeBackend (fully offline)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"


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
    }
    body.update(overrides)
    return body


class TestHealth:
    def test_health_is_open_and_reports_backend(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["version"]
        assert body["backend"] == {"type": "fake", "status": "ready"}

    def test_health_needs_no_auth_even_when_key_configured(self) -> None:
        client = _client(_settings(), FakeBackend())
        assert client.get("/health").status_code == 200


class TestModels:
    def test_lists_the_gateway_alias(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.get("/v1/models", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert [m["id"] for m in body["data"]] == [MODEL]
        assert body["data"][0]["object"] == "model"

    def test_custom_model_id_is_advertised(self) -> None:
        client = _client(_settings(model_id="my-alias"), FakeBackend())
        response = client.get("/v1/models", headers=AUTH)
        assert [m["id"] for m in response.json()["data"]] == ["my-alias"]

    def test_requires_auth(self) -> None:
        client = _client(_settings(), FakeBackend())
        assert client.get("/v1/models").status_code == 401


class TestChatCompletionsSuccess:
    def test_plain_chat_returns_openai_shape(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("Hello!")])
        client = _client(_settings(), backend)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl_local_")
        assert body["model"] == MODEL
        assert isinstance(body["created"], int)
        assert len(body["choices"]) == 1
        assert body["choices"][0]["index"] == 0
        assert body["choices"][0]["message"] == {
            "role": "assistant",
            "content": "Hello!",
        }
        assert body["choices"][0]["finish_reason"] == "stop"

    def test_multiple_text_deltas_are_concatenated(self) -> None:
        backend = FakeBackend(
            turns=[[MessageStarted(), TextDelta("He"), TextDelta("llo"), MessageFinished("stop")]]
        )
        client = _client(_settings(), backend)
        body = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH).json()
        assert body["choices"][0]["message"]["content"] == "Hello"

    def test_finish_reason_length_passes_through(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("x", finish_reason="length")])
        client = _client(_settings(), backend)
        body = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH).json()
        assert body["choices"][0]["finish_reason"] == "length"

    def test_missing_finish_reason_maps_to_stop(self) -> None:
        backend = FakeBackend(
            turns=[[MessageStarted(), TextDelta("x"), MessageFinished(None)]]
        )
        client = _client(_settings(), backend)
        body = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH).json()
        assert body["choices"][0]["finish_reason"] == "stop"

    def test_compiled_prompt_is_passed_to_the_backend(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        payload = _chat_body(
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Hello"},
            ]
        )
        assert client.post("/v1/chat/completions", json=payload, headers=AUTH).status_code == 200
        assert backend.turn_calls[0].prompt == "[system]\nBe brief.\n\n[user]\nHello"

    def test_unknown_request_fields_are_tolerated(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        payload = _chat_body(
            temperature=0.2,
            max_tokens=32768,
            user="qwen-code",
            reasoning_effort="high",
            enable_thinking=False,
        )
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 200

    def test_content_parts_are_compiled(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        payload = _chat_body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "image_url", "image_url": {"url": "x"}},
                        {"type": "text", "text": "b"},
                    ],
                }
            ]
        )
        assert client.post("/v1/chat/completions", json=payload, headers=AUTH).status_code == 200
        assert backend.turn_calls[0].prompt == "[user]\na\nb"

    def test_repeated_identical_request_is_a_new_conversation_and_session(self) -> None:
        # M4 note: session reuse applies to CONTINUATIONS (request history
        # strictly extends stored history, ADR-020). Re-sending the same
        # single user message is a duplicate, not a continuation — the
        # stored history already contains the assistant reply — so it still
        # starts a fresh conversation and backend session.
        backend = FakeBackend(turns=[fake_text_turn("1"), fake_text_turn("2")])
        client = _client(_settings(), backend)
        assert client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH).status_code == 200
        assert client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH).status_code == 200
        assert [s.session_id for s in backend.sessions_created] == [
            "fake-session-1",
            "fake-session-2",
        ]
        assert backend.turn_calls[0].session_id == "fake-session-1"
        assert backend.turn_calls[1].session_id == "fake-session-2"


class TestChatCompletionsRejections:
    def test_stream_true_now_streams_sse(self) -> None:
        # M2 rejected stream=true with 501; M3 implemented streaming — the
        # full behavior lives in tests/test_api_streaming.py.
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions", json=_chat_body(stream=True), headers=AUTH
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

    def test_tools_are_400_until_m6(self) -> None:
        client = _client(_settings(), FakeBackend())
        payload = _chat_body(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TOOLS_NOT_YET_SUPPORTED"

    def test_tool_choice_alone_is_400_until_m6(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions", json=_chat_body(tool_choice="required"), headers=AUTH
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "TOOLS_NOT_YET_SUPPORTED"

    def test_tool_message_is_400_unsupported_message(self) -> None:
        client = _client(_settings(), FakeBackend())
        payload = _chat_body(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ]
        )
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "UNSUPPORTED_MESSAGE"
        assert "M6" in error["message"]

    def test_unknown_model_is_404(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions", json=_chat_body(model="gpt-4o"), headers=AUTH
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"

    def test_empty_messages_is_422(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions", json=_chat_body(messages=[]), headers=AUTH
        )
        assert response.status_code == 422

    def test_missing_model_is_422(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )
        assert response.status_code == 422


class TestBackendFailureMapping:
    def _post(self, backend: FakeBackend):
        client = _client(_settings(), backend)
        return client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)

    def test_rate_limited_maps_to_429(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.RATE_LIMITED, message="slow down"
        )
        response = self._post(FakeBackend(turns=[[failure]]))
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "RATE_LIMITED"
        assert error["type"] == "upstream_rate_limit_error"
        assert error["message"] == "slow down"

    def test_auth_invalid_maps_to_502(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.AUTH_INVALID, message="token rejected"
        )
        response = self._post(FakeBackend(turns=[[failure]]))
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"

    def test_exhausted_fake_maps_to_500(self) -> None:
        response = self._post(FakeBackend())  # no scripted turns
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL"


class TestAuthMatrix:
    def test_missing_header_is_401(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post("/v1/chat/completions", json=_chat_body())
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_wrong_key_is_401(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_non_bearer_scheme_is_401(self) -> None:
        client = _client(_settings(), FakeBackend())
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers={"Authorization": "Basic test-key"},
        )
        assert response.status_code == 401

    def test_correct_key_is_accepted(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 200

    def test_bearer_prefix_is_case_insensitive(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(_settings(), backend)
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(),
            headers={"Authorization": "bearer test-key"},
        )
        assert response.status_code == 200

    def test_unconfigured_key_refuses_to_serve(self) -> None:
        client = _client(_settings(gateway_api_key=None), FakeBackend())
        response = client.post("/v1/chat/completions", json=_chat_body())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "GATEWAY_API_KEY_NOT_CONFIGURED"

    def test_allow_no_auth_opens_the_dev_door(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(
            _settings(gateway_api_key=None, allow_no_auth=True), backend
        )
        response = client.post("/v1/chat/completions", json=_chat_body())
        assert response.status_code == 200
