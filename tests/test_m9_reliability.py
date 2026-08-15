"""M9 tests: reliability hardening (ROADMAP M9, ADR-036).

Offline failure-injection suite proving the M9 exit criteria:

* **bounded** transport retry — retryable failures are retried up to the
  configured budget with a deterministic linear backoff; non-retryable
  failures make exactly ONE attempt (no hot loop);
* **status-preserving** final failure — after budget exhaustion the public
  error shape is identical to the no-retry mapping (retry changes latency
  before failure, never the failure's public shape);
* **strict terminal** — a turn without a MessageFinished marker is
  truncation: retryable pre-byte, an error envelope WITHOUT ``[DONE]``
  mid-stream, and never a fabricated ``stop``;
* **Cloudflare normalization** pins (category, status, type, single
  attempt);
* upstream **timeout** plumbing (config → vendor patch seam);
* **metrics** + the open ``GET /admin/metrics`` surface.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.backends.deepseek_web import _vendor  # noqa: F401  (vendor on path)
from app.backends.deepseek_web.normalize import classify_upstream_exception
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend, fake_text_turn
from app.config import ConfigError, GatewaySettings
from app.metrics import MetricsCollector
from app.reliability import RetryPolicy, with_transport_retry
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

HEALTHY_TURN = [
    MessageStarted(),
    TextDelta("Hel"),
    TextDelta("lo!"),
    MessageFinished("stop"),
]


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _client(
    backend: FakeBackend,
    settings: GatewaySettings | None = None,
    metrics: MetricsCollector | None = None,
) -> TestClient:
    return TestClient(create_app(settings or _settings(), backend, metrics=metrics))


def _chat_body(**overrides) -> dict:
    body: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": False,
    }
    body.update(overrides)
    return body


def _stream(client: TestClient, payload: dict) -> tuple[int, list[str]]:
    with client.stream(
        "POST", "/v1/chat/completions", json=payload, headers=AUTH
    ) as response:
        lines = [line for line in response.iter_lines() if line.strip()]
        return response.status_code, lines


def _parse(line: str) -> dict:
    assert line.startswith("data: "), f"unexpected SSE framing: {line!r}"
    return json.loads(line[len("data: ") :])


def _rate_limited(message: str = "slow down") -> BackendFailure:
    return BackendFailure(
        category=BackendErrorCategory.RATE_LIMITED, message=message
    )


def _zero_backoff(**overrides) -> GatewaySettings:
    overrides.setdefault("retry_backoff_seconds", 0.0)
    return _settings(**overrides)


# ---------------------------------------------------------------------------
# with_transport_retry (unit level: budget, backoff, taxonomy, metrics)
# ---------------------------------------------------------------------------


class TestWithTransportRetryUnit:
    def test_retryable_failure_succeeds_after_one_bounded_retry(self) -> None:
        calls = {"n": 0}
        sleeps: list[float] = []
        retried: list[tuple[int, float, BackendFailure]] = []

        def fn() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _rate_limited()
            return "ok"

        metrics = MetricsCollector()
        result = with_transport_retry(
            fn,
            policy=RetryPolicy(),
            sleep=sleeps.append,
            on_retry=lambda n, delay, failure: retried.append((n, delay, failure)),
            metrics=metrics,
        )
        assert result == "ok"
        assert calls["n"] == 2
        assert sleeps == [0.5]  # linear base * retry_number, no jitter
        assert [(n, delay, f.category) for n, delay, f in retried] == [
            (1, 0.5, BackendErrorCategory.RATE_LIMITED)
        ]
        snap = metrics.snapshot()
        assert snap["backend_attempts"] == 2
        assert snap["backend_failures"] == {"RATE_LIMITED": 1}
        assert snap["transport_retries"] == 1

    def test_budget_exhaustion_reraises_the_final_failure_unchanged(
        self,
    ) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_NETWORK, message="conn reset"
        )

        def fn() -> None:
            raise failure

        sleeps: list[float] = []
        metrics = MetricsCollector()
        with pytest.raises(BackendFailure) as raised:
            with_transport_retry(
                fn,
                policy=RetryPolicy(),
                sleep=sleeps.append,
                metrics=metrics,
            )
        assert raised.value is failure  # status-preserving: same object
        assert sleeps == [0.5, 1.0]  # deterministic linear schedule
        snap = metrics.snapshot()
        assert snap["backend_attempts"] == 3  # 1 + max_retries
        assert snap["transport_retries"] == 2
        assert snap["backend_failures"] == {"UPSTREAM_NETWORK": 3}

    def test_non_retryable_failure_is_exactly_one_attempt(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.AUTH_INVALID, message="token rejected"
        )
        calls = {"n": 0}
        sleeps: list[float] = []

        def fn() -> None:
            calls["n"] += 1
            raise failure

        with pytest.raises(BackendFailure):
            with_transport_retry(fn, policy=RetryPolicy(), sleep=sleeps.append)
        assert calls["n"] == 1  # no hot loop
        assert sleeps == []

    def test_non_backend_failure_exceptions_propagate_without_retry(
        self,
    ) -> None:
        calls = {"n": 0}

        def fn() -> None:
            calls["n"] += 1
            raise ValueError("programmer error")

        with pytest.raises(ValueError):
            with_transport_retry(fn, policy=RetryPolicy(), sleep=lambda _: None)
        assert calls["n"] == 1

    def test_zero_max_retries_disables_retry_but_keeps_the_path(self) -> None:
        calls = {"n": 0}

        def fn() -> None:
            calls["n"] += 1
            raise _rate_limited()

        with pytest.raises(BackendFailure):
            with_transport_retry(
                fn, policy=RetryPolicy(max_retries=0), sleep=lambda _: None
            )
        assert calls["n"] == 1

    def test_backoff_is_linear_in_the_retry_number(self) -> None:
        sleeps: list[float] = []

        def fn() -> None:
            raise _rate_limited()

        with pytest.raises(BackendFailure):
            with_transport_retry(
                fn,
                policy=RetryPolicy(max_retries=3, backoff_seconds=0.25),
                sleep=sleeps.append,
            )
        assert sleeps == [0.25, 0.5, 0.75]


# ---------------------------------------------------------------------------
# Public retry behavior (route level, non-streaming)
# ---------------------------------------------------------------------------


class TestPublicRetryBehavior:
    def test_retryable_failure_then_recovery_is_200(self) -> None:
        backend = FakeBackend(turns=[[_rate_limited()], fake_text_turn("ok")])
        metrics = MetricsCollector()
        client = _client(backend, _zero_backoff(), metrics)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert len(backend.turn_calls) == 2
        snap = metrics.snapshot()
        # backend_attempts counts EVERY retry-wrapped upstream call: one
        # session creation + two turn attempts.
        assert snap["backend_attempts"] == 3
        assert snap["transport_retries"] == 1
        assert snap["backend_failures"] == {"RATE_LIMITED": 1}

    def test_non_retryable_failure_is_one_attempt_no_hot_loop(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.AUTH_INVALID, message="token rejected"
        )
        backend = FakeBackend(turns=[[failure]])
        metrics = MetricsCollector()
        client = _client(backend, _zero_backoff(), metrics)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"
        assert len(backend.turn_calls) == 1
        # One session-creation attempt + one turn attempt; zero retries.
        assert metrics.snapshot()["backend_attempts"] == 2
        assert metrics.snapshot()["transport_retries"] == 0

    def test_budget_exhaustion_keeps_the_no_retry_status_shape(self) -> None:
        failure = _rate_limited()
        backend = FakeBackend(turns=[[failure], [failure], [failure]])
        metrics = MetricsCollector()
        client = _client(backend, _zero_backoff(), metrics)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        # Identical to the pre-M9 no-retry mapping (ADR-036: retry changes
        # latency before failure, never the failure's public shape).
        assert response.status_code == 429
        error = response.json()["error"]
        assert error == {
            "message": "slow down",
            "type": "upstream_rate_limit_error",
            "code": "RATE_LIMITED",
        }
        assert len(backend.turn_calls) == 3
        snap = metrics.snapshot()
        assert snap["backend_attempts"] == 4  # one session + three turn attempts
        assert snap["transport_retries"] == 2
        assert snap["backend_failures"] == {"RATE_LIMITED": 3}

    def test_retryable_upstream_5xx_recovers_within_budget(self) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_5XX, message="boom"
        )
        backend = FakeBackend(turns=[[failure], [failure], fake_text_turn("ok")])
        client = _client(backend, _zero_backoff())
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 200
        assert len(backend.turn_calls) == 3


# ---------------------------------------------------------------------------
# Streaming retry + strict terminal
# ---------------------------------------------------------------------------


class TestStreamingRetryAndStrictTerminal:
    def test_retryable_pre_byte_failure_then_healthy_stream(self) -> None:
        backend = FakeBackend(turns=[[_rate_limited()], HEALTHY_TURN])
        client = _client(backend, _zero_backoff())
        status, lines = _stream(client, _chat_body(stream=True))
        assert status == 200
        assert lines[-1] == "data: [DONE]"
        contents = [
            _parse(line)["choices"][0]["delta"].get("content")
            for line in lines
            if line != "data: [DONE]"
        ]
        assert [c for c in contents if c] == ["Hel", "lo!"]
        assert len(backend.turn_calls) == 2

    def test_eventless_turn_recovers_on_the_second_attempt(self) -> None:
        # A zero-event turn fails at priming (pre-byte) → truncation is
        # retryable → attempt 2 streams normally.
        backend = FakeBackend(turns=[[], HEALTHY_TURN])
        client = _client(backend, _zero_backoff())
        status, lines = _stream(client, _chat_body(stream=True))
        assert status == 200
        assert lines[-1] == "data: [DONE]"
        assert len(backend.turn_calls) == 2

    def test_eventless_turn_budget_exhaustion_is_502_protocol(self) -> None:
        backend = FakeBackend(turns=[[], [], []])
        client = _client(backend, _zero_backoff())
        response = client.post(
            "/v1/chat/completions", json=_chat_body(), headers=AUTH
        )
        assert response.status_code == 502
        error = response.json()["error"]
        assert error["code"] == "UPSTREAM_PROTOCOL"
        assert error["type"] == "upstream_protocol_error"
        assert "terminal marker" in error["message"]
        assert len(backend.turn_calls) == 3

    def test_markerless_turn_after_healthy_events_recovers(self) -> None:
        # Truncation pre-byte also covers turns whose FIRST attempt produced
        # nothing usable at priming — here scripted as an eventless turn
        # followed by a fully healthy one on the non-streaming path.
        backend = FakeBackend(turns=[[], fake_text_turn("recovered")])
        client = _client(backend, _zero_backoff())
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "recovered"

    def test_mid_stream_truncation_is_an_error_envelope_without_done(
        self,
    ) -> None:
        # HTTP 200 is already committed → mid-stream rules: NO retry, an
        # in-stream error envelope, and no [DONE] (the stream never
        # terminated cleanly, so nothing may claim it did).
        turn = [MessageStarted(), TextDelta("par")]  # no MessageFinished
        backend = FakeBackend(turns=[turn])
        client = _client(backend, _zero_backoff())
        status, lines = _stream(client, _chat_body(stream=True))
        assert status == 200
        assert "data: [DONE]" not in lines
        last = _parse(lines[-1])
        assert last["error"]["code"] == "UPSTREAM_PROTOCOL"
        assert "terminal marker" in last["error"]["message"]
        # The partial content still arrived before the truncation.
        assert _parse(lines[1])["choices"][0]["delta"]["content"] == "par"
        assert len(backend.turn_calls) == 1  # mid-stream failures never retry


# ---------------------------------------------------------------------------
# Buffered tool turns stay inside the retry wrapper
# ---------------------------------------------------------------------------


class TestBufferedToolTurnRetry:
    READ_TOOL = {
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

    def test_buffered_tool_turn_truncation_exhausts_the_budget(self) -> None:
        backend = FakeBackend(turns=[[], [], []])
        client = _client(backend, _zero_backoff())
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[self.READ_TOOL], tool_choice="required"),
            headers=AUTH,
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "UPSTREAM_PROTOCOL"
        assert len(backend.turn_calls) == 3  # bounded, never a hot loop


# ---------------------------------------------------------------------------
# Cloudflare normalization (M9 pins)
# ---------------------------------------------------------------------------


class TestCloudflareNormalization:
    def test_vendor_cloudflare_error_classifies_blocked_non_retryable(
        self,
    ) -> None:
        from dsk import api as dsk_api  # vendored (tests/ is boundary-exempt)

        failure = classify_upstream_exception(dsk_api.CloudflareError("blocked"))
        assert failure.category is BackendErrorCategory.CLOUDFLARE_BLOCKED
        assert failure.retryable is False

    def test_api_error_mentioning_cloudflare_classifies_blocked(self) -> None:
        from dsk import api as dsk_api

        failure = classify_upstream_exception(
            dsk_api.APIError("Cloudflare challenge could not be satisfied")
        )
        assert failure.category is BackendErrorCategory.CLOUDFLARE_BLOCKED
        assert failure.retryable is False

    def test_cloudflare_block_maps_to_503_unavailable_in_one_attempt(
        self,
    ) -> None:
        failure = BackendFailure(
            category=BackendErrorCategory.CLOUDFLARE_BLOCKED, message="blocked"
        )
        backend = FakeBackend(turns=[[failure]])
        metrics = MetricsCollector()
        client = _client(backend, _zero_backoff(), metrics)
        response = client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["type"] == "upstream_unavailable_error"
        assert error["code"] == "CLOUDFLARE_BLOCKED"
        assert len(backend.turn_calls) == 1  # non-retryable → single attempt
        assert metrics.snapshot()["transport_retries"] == 0


# ---------------------------------------------------------------------------
# Timeout plumbing (config → vendor patch seam, offline)
# ---------------------------------------------------------------------------


class TestTimeoutPlumbing:
    def test_backend_sets_the_vendor_default_timeout(self) -> None:
        from dsk import api as dsk_api

        from app.backends.deepseek_web.backend import DeepSeekWebBackend

        original = dsk_api.DEFAULT_REQUEST_TIMEOUT
        try:
            dsk_api.DEFAULT_REQUEST_TIMEOUT = None
            DeepSeekWebBackend("dummy-offline-token", request_timeout=42.5)
            assert dsk_api.DEFAULT_REQUEST_TIMEOUT == 42.5
        finally:
            dsk_api.DEFAULT_REQUEST_TIMEOUT = original

    def test_backend_without_timeout_leaves_the_vendor_default(self) -> None:
        from dsk import api as dsk_api

        from app.backends.deepseek_web.backend import DeepSeekWebBackend

        original = dsk_api.DEFAULT_REQUEST_TIMEOUT
        try:
            dsk_api.DEFAULT_REQUEST_TIMEOUT = 99.0
            DeepSeekWebBackend("dummy-offline-token")
            assert dsk_api.DEFAULT_REQUEST_TIMEOUT == 99.0
        finally:
            dsk_api.DEFAULT_REQUEST_TIMEOUT = original

    def test_env_timeout_reaches_settings(self) -> None:
        settings = GatewaySettings.from_env(
            {"GATEWAY_BACKEND": "fake", "DSQG_UPSTREAM_TIMEOUT_SECONDS": "30"}
        )
        assert settings.upstream_timeout_seconds == 30.0


# ---------------------------------------------------------------------------
# Metrics + GET /admin/metrics
# ---------------------------------------------------------------------------


class TestMetricsSurface:
    EXPECTED_SNAPSHOT_KEYS = {
        "requests",
        "request_seconds",
        "backend_attempts",
        "backend_failures",
        "transport_retries",
        "tool_turns",
        "tool_repair_retries",
        "tool_repair_budget_exhausted",
        "backend_attempt_seconds",
        "uptime_seconds",
    }

    def test_admin_metrics_is_open_and_shaped(self) -> None:
        client = _client(FakeBackend())
        response = client.get("/admin/metrics")  # no auth header
        assert response.status_code == 200
        snap = response.json()
        assert self.EXPECTED_SNAPSHOT_KEYS <= set(snap)
        assert snap["uptime_seconds"] >= 0

    def test_requests_are_counted_by_endpoint_and_status_class(self) -> None:
        backend = FakeBackend(turns=[fake_text_turn("ok")])
        client = _client(backend)
        assert (
            client.post("/v1/chat/completions", json=_chat_body(), headers=AUTH)
            .status_code
            == 200
        )
        assert (
            client.post("/v1/chat/completions", json=_chat_body()).status_code == 401
        )
        snap = client.get("/admin/metrics").json()
        assert snap["requests"]["POST /v1/chat/completions"] == {"2xx": 1, "4xx": 1}
        assert snap["request_seconds"]["count"] == 2
        assert snap["backend_attempts"] == 2  # session creation + turn

    def test_tool_turn_and_repair_metrics_are_recorded(self) -> None:
        # tools[] + two plain answers: ONE tool turn, ONE bounded repair
        # retry, budget exhausted → honest text fallback (ADR-029/035).
        backend = FakeBackend(
            turns=[fake_text_turn("plain one"), fake_text_turn("plain two")]
        )
        client = _client(backend)
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body(tools=[TestBufferedToolTurnRetry.READ_TOOL]),
            headers=AUTH,
        )
        assert response.status_code == 200
        snap = client.get("/admin/metrics").json()
        assert snap["tool_turns"] == 1
        assert snap["tool_repair_retries"] == 1
        assert snap["tool_repair_budget_exhausted"] == 1


# ---------------------------------------------------------------------------
# Retry/timeout configuration parsing
# ---------------------------------------------------------------------------


class TestRetryConfigParsing:
    def test_defaults(self) -> None:
        settings = _settings()
        assert settings.max_retries == 2
        assert settings.retry_backoff_seconds == 0.5
        assert settings.upstream_timeout_seconds == 60.0

    def test_env_overrides_parse(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "GATEWAY_BACKEND": "fake",
                "GATEWAY_MAX_RETRIES": "0",
                "GATEWAY_RETRY_BACKOFF_SECONDS": "0.25",
                "DSQG_UPSTREAM_TIMEOUT_SECONDS": "45.5",
            }
        )
        assert settings.max_retries == 0
        assert settings.retry_backoff_seconds == 0.25
        assert settings.upstream_timeout_seconds == 45.5

    @pytest.mark.parametrize(
        ("var", "raw"),
        [
            ("GATEWAY_MAX_RETRIES", "-1"),
            ("GATEWAY_MAX_RETRIES", "abc"),
            ("GATEWAY_RETRY_BACKOFF_SECONDS", "-0.5"),
            ("GATEWAY_RETRY_BACKOFF_SECONDS", "abc"),
            ("DSQG_UPSTREAM_TIMEOUT_SECONDS", "0"),
            ("DSQG_UPSTREAM_TIMEOUT_SECONDS", "-5"),
            ("DSQG_UPSTREAM_TIMEOUT_SECONDS", "abc"),
        ],
    )
    def test_invalid_values_raise_config_error(self, var: str, raw: str) -> None:
        with pytest.raises(ConfigError):
            GatewaySettings.from_env({"GATEWAY_BACKEND": "fake", var: raw})
