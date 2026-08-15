"""M5 tests: opt-in diagnostic request capture (app/diagnostics.py).

The capture layer exists so a real Qwen Code connection can be fixtured:
sanitized request records land in ``<dir>/requests.jsonl``. Privacy pins:
capture is opt-in, and the Authorization header VALUE is never written.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.fake import FakeBackend, fake_text_turn
from app.config import GatewaySettings
from app.diagnostics import REQUESTS_FILE_NAME, RequestRecorder
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(settings: GatewaySettings, backend: FakeBackend) -> TestClient:
    return TestClient(create_app(settings, backend))


def _read_records(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestRequestRecorderUnit:
    def test_writes_one_jsonl_record_per_call(self, tmp_path: Path) -> None:
        recorder = RequestRecorder(tmp_path)
        recorder.record(
            "POST",
            "/v1/chat/completions",
            headers={
                "content-type": "application/json",
                "user-agent": "QwenCode/0.21.11",
                "authorization": "Bearer super-secret-value",
            },
            body={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        )
        recorder.record(
            "POST", "/v1/chat/completions", headers={}, body={"model": MODEL}
        )
        records = _read_records(tmp_path / REQUESTS_FILE_NAME)
        assert len(records) == 2
        first = records[0]
        assert first["method"] == "POST"
        assert first["path"] == "/v1/chat/completions"
        assert first["ts"]
        assert first["headers"]["content_type"] == "application/json"
        assert first["headers"]["user_agent"] == "QwenCode/0.21.11"
        assert first["headers"]["authorization"] == "present"
        assert first["body"]["messages"][0]["content"] == "hi"
        assert records[1]["headers"]["authorization"] == "absent"

    def test_authorization_value_is_never_written(self, tmp_path: Path) -> None:
        recorder = RequestRecorder(tmp_path)
        recorder.record(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer super-secret-value"},
            body={"model": MODEL},
        )
        raw = (tmp_path / REQUESTS_FILE_NAME).read_text(encoding="utf-8")
        assert "super-secret-value" not in raw
        assert "Bearer" not in raw

    def test_unsafe_header_values_are_dropped(self, tmp_path: Path) -> None:
        recorder = RequestRecorder(tmp_path)
        recorder.record(
            "POST",
            "/v1/chat/completions",
            headers={"x-custom-trace": "trace-123", "cookie": "session=abc"},
            body={},
        )
        raw = (tmp_path / REQUESTS_FILE_NAME).read_text(encoding="utf-8")
        assert "trace-123" not in raw
        assert "session=abc" not in raw


class TestCaptureDisabledByDefault:
    def test_no_recorder_without_diagnostics_dir(self) -> None:
        app = create_app(_settings(), FakeBackend())
        assert app.state.recorder is None

    def test_from_env_defaults_to_disabled(self) -> None:
        settings = GatewaySettings.from_env(
            {"GATEWAY_BACKEND": "fake", "GATEWAY_DIAGNOSTICS_DIR": ""}
        )
        assert settings.diagnostics_dir is None


class TestCaptureIntegration:
    def test_successful_request_is_captured(self, tmp_path: Path) -> None:
        # ADR-029: this tool-enabled PRE-loop turn pays one bounded
        # repair retry, so two turns are scripted; capture is
        # REQUEST-only and per-HTTP-request, so exactly ONE record.
        backend = FakeBackend(turns=[fake_text_turn("ok"), fake_text_turn("ok")])
        client = _client(_settings(diagnostics_dir=tmp_path), backend)
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 200

        records = _read_records(tmp_path / REQUESTS_FILE_NAME)
        assert len(records) == 1
        body = records[0]["body"]
        assert body["model"] == MODEL
        assert body["stream"] is False
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        assert body["tools"][0]["function"]["name"] == "read_file"
        assert records[0]["headers"]["authorization"] == "present"

        raw = (tmp_path / REQUESTS_FILE_NAME).read_text(encoding="utf-8")
        assert "test-key" not in raw

    def test_rejected_requests_are_captured_too(self, tmp_path: Path) -> None:
        # Capture happens BEFORE validation: rejected shapes (here: a
        # null-content assistant message WITHOUT tool_calls, still 400
        # after M6) are exactly what the wire fixtures need.
        client = _client(_settings(diagnostics_dir=tmp_path), FakeBackend())
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None},
            ],
        }
        response = client.post("/v1/chat/completions", json=payload, headers=AUTH)
        assert response.status_code == 400
        records = _read_records(tmp_path / REQUESTS_FILE_NAME)
        assert len(records) == 1
        assert records[0]["body"]["messages"][1]["role"] == "assistant"
        # The capture dump uses exclude_none (raw wire fidelity), so the
        # null content is absent rather than JSON null.
        assert "content" not in records[0]["body"]["messages"][1]

    def test_requests_append(self, tmp_path: Path) -> None:
        backend = FakeBackend(
            turns=[fake_text_turn("one"), fake_text_turn("two")]
        )
        client = _client(_settings(diagnostics_dir=tmp_path), backend)
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        assert client.post("/v1/chat/completions", json=payload, headers=AUTH).status_code == 200
        assert client.post("/v1/chat/completions", json=payload, headers=AUTH).status_code == 200
        assert len(_read_records(tmp_path / REQUESTS_FILE_NAME)) == 2
