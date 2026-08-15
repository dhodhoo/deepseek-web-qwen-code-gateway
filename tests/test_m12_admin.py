"""M12 tests (ADR-039): admin UI + account lifecycle management.

Offline suite proving the M12 exit criteria — "credentials are masked;
account lifecycle is manageable; core API remains independent of UI":

* ``GET /admin`` serves ONE self-contained HTML dashboard (no external
  assets) that is a pure client of the ``/admin/*`` JSON endpoints;
* ``GET /admin/summary`` aggregates fleet health (the exact ``/health``
  payload), per-state account counts, conversation/session counts and
  headline metrics;
* ``GET /admin/sessions`` serializes conversation METADATA only — a
  distinctive prompt/tool-output marker must NOT appear on ANY admin
  surface (masking sweep);
* ``GET /admin/settings`` renders secrets as PRESENCE ONLY (auth mode,
  account count) — never values;
* the lifecycle mutations (``POST /admin/accounts/{id}/enable|disable|
  reset``) drive the M10 router seams: disable releases session links
  and routing avoids the account, enable flips the flag only (invalid
  stays invalid), reset restores invalid/cooling accounts;
* unknown account ids answer 404 ``ACCOUNT_NOT_FOUND``;
* ``/v1/*``, ``/health`` and the pre-M12 admin payloads stay stable
  through every admin operation (core independence).

Everything runs offline against scripted ``FakeBackend`` instances.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.accounts import AccountRecord, AccountRouter
from app.admin import build_settings_view
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend
from app.config import DeepSeekWebSettings, GatewaySettings
from app.conversation import ConversationStore
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

#: Distinctive markers that must never leak onto any admin surface.
PROMPT_MARKER = "MARKER-SECRET-PROMPT-9f3k"
TOOL_OUTPUT_MARKER = "MARKER-TOOL-OUTPUT-77xz"

ADMIN_READ_ENDPOINTS = (
    "/admin",
    "/admin/summary",
    "/admin/accounts",
    "/admin/sessions",
    "/admin/settings",
    "/admin/metrics",
)


def _settings(**overrides) -> GatewaySettings:
    base: dict = {
        "backend_type": "fake",
        "gateway_api_key": SecretStr("test-key"),
        "retry_backoff_seconds": 0.0,
    }
    base.update(overrides)
    return GatewaySettings(**base)


def _multi_client(
    backends: list[FakeBackend],
    *,
    cooldown: float = 300.0,
    settings: GatewaySettings | None = None,
    store: ConversationStore | None = None,
) -> tuple[TestClient, AccountRouter, ConversationStore]:
    router = AccountRouter(
        [
            AccountRecord(
                id=f"acct-{index}", label=f"Account {index}", backend=backend
            )
            for index, backend in enumerate(backends, start=1)
        ],
        cooldown_seconds=cooldown,
    )
    store = store or ConversationStore()
    app = create_app(settings or _settings(), store=store, router=router)
    return TestClient(app), router, store


def _turn(text: str) -> list:
    return [MessageStarted(), TextDelta(text), MessageFinished("stop")]


def _failure(category: BackendErrorCategory) -> BackendFailure:
    return BackendFailure(category=category, message="scripted")


def _chat(messages: list) -> dict:
    return {"model": MODEL, "messages": messages, "stream": False}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _assistant_tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _account_row(client: TestClient, account_id: str) -> dict:
    response = client.get("/admin/accounts")
    assert response.status_code == 200
    for row in response.json()["accounts"]:
        if row["id"] == account_id:
            return row
    raise AssertionError(f"no account row {account_id!r}")


# ---------------------------------------------------------------------------
# The dashboard page (GET /admin)
# ---------------------------------------------------------------------------


class TestAdminPage:
    def test_admin_page_is_self_contained_html(self) -> None:
        client, _, _ = _multi_client([FakeBackend()])
        response = client.get("/admin")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        html = response.text
        # All six roadmap sections (System health lives on the
        # Dashboard tab via the /health payload card).
        for tab in ("Dashboard", "Accounts", "Sessions", "Metrics", "Settings"):
            assert tab in html
        # Self-contained: no external assets — the page must work
        # offline on a local gateway.
        assert 'src="http' not in html
        assert 'href="http' not in html

    def test_admin_page_is_a_pure_client_of_the_json_endpoints(self) -> None:
        client, _, _ = _multi_client([FakeBackend()])
        html = client.get("/admin").text
        for endpoint in (
            "/admin/summary",
            "/admin/accounts",
            "/admin/sessions",
            "/admin/settings",
            "/admin/metrics",
        ):
            assert endpoint in html
        # Lifecycle mutation URL pattern (the JS builds the final path).
        assert "/admin/accounts/\"" in html or "/admin/accounts/" in html


# ---------------------------------------------------------------------------
# Dashboard aggregate (GET /admin/summary)
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_shape_and_live_counts(self) -> None:
        b1 = FakeBackend(turns=[_turn("A1")])
        b2 = FakeBackend()
        client, _, _ = _multi_client([b1, b2])

        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        assert response.status_code == 200

        summary = client.get("/admin/summary").json()
        # The dashboard health card IS the /health payload.
        assert summary["health"] == client.get("/health").json()
        assert summary["backend_type"] == "fake"
        assert summary["accounts"]["total"] == 2
        assert summary["accounts"]["by_state"]["healthy"] == 2
        assert summary["accounts"]["by_state"]["disabled"] == 0
        assert summary["conversations"] == 1
        assert summary["active_sessions"] == 1
        assert summary["uptime_seconds"] >= 0
        metrics = summary["metrics"]
        assert metrics["requests"]["POST /v1/chat/completions"]["2xx"] == 1
        # Session creation + turn drain, both retry-wrapped.
        assert metrics["backend_attempts"] >= 2
        assert metrics["session_failovers"] == 0

    def test_summary_health_tracks_fleet_state(self) -> None:
        client, _, _ = _multi_client([FakeBackend()])
        assert client.get("/admin/summary").json()["health"]["ok"] is True
        # Disable the only account: fleet-aware /health flips, and the
        # summary card follows it exactly.
        client.post("/admin/accounts/acct-1/disable")
        health = client.get("/health").json()
        assert health["ok"] is False
        summary = client.get("/admin/summary").json()
        assert summary["health"] == health
        assert summary["accounts"]["by_state"]["disabled"] == 1


# ---------------------------------------------------------------------------
# Sessions view (GET /admin/sessions) — metadata only, never content
# ---------------------------------------------------------------------------


class TestSessionsView:
    def test_sessions_empty_initially(self) -> None:
        client, _, _ = _multi_client([FakeBackend()])
        assert client.get("/admin/sessions").json() == {"sessions": []}

    def test_sessions_rows_are_metadata_only(self) -> None:
        b1 = FakeBackend(turns=[_turn("done")])
        client, _, store = _multi_client([b1])
        messages = [
            _user(PROMPT_MARKER),
            _assistant_tool_call("call_x", "read_file", '{"file_path":"a.py"}'),
            _tool_result("call_x", TOOL_OUTPUT_MARKER),
            _user("next"),
        ]
        response = client.post(
            "/v1/chat/completions", json=_chat(messages), headers=AUTH
        )
        assert response.status_code == 200

        sessions = client.get("/admin/sessions").json()["sessions"]
        assert len(sessions) == 1
        row = sessions[0]
        assert row["conversation_id"].startswith("conv_")
        assert row["backend_account_id"] == "acct-1"
        assert row["backend_session_id"] == "fake-session-1"
        assert row["linked"] is True
        assert row["status"] == "active"
        # incoming 4 + assistant reply 1
        assert row["message_count"] == 5
        assert row["tool_call_count"] == 1
        assert isinstance(row["created_at"], float)
        assert isinstance(row["updated_at"], float)

    def test_masking_sweep_conversation_content_never_on_admin_surface(
        self,
    ) -> None:
        # EXIT criterion "credentials are masked" — generalized to every
        # secret-class value: prompts and tool output are never
        # serialized by ANY admin surface.
        b1 = FakeBackend(turns=[_turn("done")])
        client, _, _ = _multi_client([b1])
        messages = [
            _user(PROMPT_MARKER),
            _assistant_tool_call("call_x", "read_file", '{"file_path":"a.py"}'),
            _tool_result("call_x", TOOL_OUTPUT_MARKER),
            _user("next"),
        ]
        assert (
            client.post(
                "/v1/chat/completions", json=_chat(messages), headers=AUTH
            ).status_code
            == 200
        )
        for endpoint in ADMIN_READ_ENDPOINTS:
            body = client.get(endpoint).text
            assert PROMPT_MARKER not in body, endpoint
            assert TOOL_OUTPUT_MARKER not in body, endpoint


# ---------------------------------------------------------------------------
# Settings view (GET /admin/settings) — presence-only secrets
# ---------------------------------------------------------------------------


class TestSettingsView:
    def test_settings_view_masks_secrets_to_presence_only(self) -> None:
        settings = GatewaySettings(
            backend_type="fake",
            gateway_api_key=SecretStr("super-secret-gateway-key"),
            deepseek_accounts=(
                DeepSeekWebSettings(auth_token=SecretStr("super-secret-token-1")),
                DeepSeekWebSettings(auth_token=SecretStr("super-secret-token-2")),
            ),
        )
        view = build_settings_view(settings)
        assert view["gateway_auth"] == "configured"
        assert view["accounts"] == {"mode": "multi", "count": 2}
        dump = json.dumps(view)
        assert "super-secret" not in dump

    def test_settings_view_auth_modes(self) -> None:
        assert (
            build_settings_view(GatewaySettings(backend_type="fake"))[
                "gateway_auth"
            ]
            == "unset"
        )
        assert (
            build_settings_view(
                GatewaySettings(backend_type="fake", allow_no_auth=True)
            )["gateway_auth"]
            == "open"
        )

    def test_settings_endpoint_echoes_knobs_without_values(self) -> None:
        client, _, _ = _multi_client(
            [FakeBackend()],
            settings=_settings(max_retries=1, upstream_timeout_seconds=45.0),
        )
        response = client.get("/admin/settings")
        assert response.status_code == 200
        view = response.json()
        assert view["backend_type"] == "fake"
        assert view["model_id"] == MODEL
        assert view["gateway_auth"] == "configured"
        assert view["accounts"] == {"mode": "single", "count": 1}
        assert view["diagnostics"] == {"enabled": False, "dir": None}
        assert view["reliability"]["max_retries"] == 1
        assert view["reliability"]["upstream_timeout_seconds"] == 45.0
        # The configured gateway key never appears on the wire.
        assert "test-key" not in response.text


# ---------------------------------------------------------------------------
# Account lifecycle (POST /admin/accounts/{id}/enable|disable|reset)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_disable_releases_links_and_routing_avoids_account(self) -> None:
        b1 = FakeBackend(turns=[_turn("A1")])
        b2 = FakeBackend(turns=[_turn("B1")])
        client, _, store = _multi_client([b1, b2])
        assert (
            client.post(
                "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
            ).status_code
            == 200
        )
        conversation = store.conversations()[0]
        assert conversation.backend_account_id == "acct-1"
        assert conversation.backend_session_id == "fake-session-1"

        response = client.post("/admin/accounts/acct-1/disable")
        assert response.status_code == 200
        row = response.json()["account"]
        assert row["id"] == "acct-1"
        assert row["enabled"] is False
        assert row["state"] == "disabled"
        # Disable releases the account's session links.
        assert conversation.backend_session_id is None
        sessions = client.get("/admin/sessions").json()["sessions"]
        assert sessions[0]["linked"] is False

        # The continuation rebuilds on the other account (full history).
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("alpha"), _assistant("A1"), _user("beta")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "B1"
        assert conversation.backend_account_id == "acct-2"
        assert len(b2.sessions_created) == 1
        assert "alpha" in b2.turn_calls[0].prompt

    def test_enable_after_disable_restores_routing(self) -> None:
        b1 = FakeBackend(turns=[_turn("A1")])
        client, _, _ = _multi_client([b1])
        assert (
            client.post("/admin/accounts/acct-1/disable").json()["account"][
                "state"
            ]
            == "disabled"
        )
        # No usable account left: router-level failure, no backend call.
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"
        assert len(b1.sessions_created) == 0

        row = client.post("/admin/accounts/acct-1/enable").json()["account"]
        assert row["enabled"] is True
        assert row["state"] == "healthy"
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A1"

    def test_enable_does_not_clear_invalid(self) -> None:
        # Enable flips the operator flag ONLY — restoration after
        # credential rotation is the explicit reset action.
        b1 = FakeBackend(
            turns=[[_failure(BackendErrorCategory.AUTH_INVALID)], _turn("back")]
        )
        client, _, _ = _multi_client([b1])
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        # Final AUTH_INVALID marks the account invalid (surfaces as 502).
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"
        assert _account_row(client, "acct-1")["state"] == "invalid"

        row = client.post("/admin/accounts/acct-1/enable").json()["account"]
        assert row["enabled"] is True
        assert row["state"] == "invalid"

    def test_reset_restores_invalid_account(self) -> None:
        b1 = FakeBackend(
            turns=[[_failure(BackendErrorCategory.AUTH_INVALID)], _turn("back")]
        )
        client, _, _ = _multi_client([b1])
        assert (
            client.post(
                "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
            ).status_code
            == 502
        )
        assert _account_row(client, "acct-1")["state"] == "invalid"
        row = client.post("/admin/accounts/acct-1/reset").json()["account"]
        assert row["enabled"] is True
        assert row["state"] == "healthy"
        assert row["consecutive_failures"] == 0
        # After rotation the account serves again.
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("gamma")]), headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "back"

    def test_reset_clears_cooldown_window(self) -> None:
        # RATE_LIMITED is retryable: the full 3-attempt budget consumes
        # three scripted turns before the final 429.
        b1 = FakeBackend(
            turns=[
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                _turn("later"),
            ]
        )
        b2 = FakeBackend(turns=[_turn("B1")])
        client, _, _ = _multi_client([b1, b2], cooldown=300.0)
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        # The M11 failover absorbs the final 429 transparently.
        assert response.status_code == 200
        row = _account_row(client, "acct-1")
        assert row["state"] == "cooldown"
        assert row["cooldown_remaining_seconds"] > 0

        row = client.post("/admin/accounts/acct-1/reset").json()["account"]
        assert row["state"] == "healthy"
        assert row["cooldown_remaining_seconds"] == 0.0
        # The window no longer blocks new conversations on the account.
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("delta")]), headers=AUTH
        )
        assert response.status_code == 200

    def test_unknown_account_id_answers_404(self) -> None:
        client, _, _ = _multi_client([FakeBackend()])
        for action in ("disable", "enable", "reset"):
            response = client.post(f"/admin/accounts/nope/{action}")
            assert response.status_code == 404, action
            error = response.json()["error"]
            assert error["code"] == "ACCOUNT_NOT_FOUND"
            assert error["type"] == "invalid_request_error"
            assert "nope" in error["message"]

    def test_single_account_deployment_lifecycle(self) -> None:
        # The backward-compatible one-account router (id "default") is
        # manageable through the exact same endpoints.
        app = create_app(_settings(), backend=FakeBackend(turns=[_turn("x")]))
        client = TestClient(app)
        row = client.post("/admin/accounts/default/disable").json()["account"]
        assert row["state"] == "disabled"
        row = client.post("/admin/accounts/default/reset").json()["account"]
        assert row["state"] == "healthy"
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Core API independence
# ---------------------------------------------------------------------------


class TestCoreIndependence:
    def test_core_api_stable_through_admin_operations(self) -> None:
        b1 = FakeBackend(turns=[_turn("A1"), _turn("A2")])
        b2 = FakeBackend()
        client, _, _ = _multi_client([b1, b2])

        health_before = client.get("/health").json()
        account_row_keys = set(_account_row(client, "acct-1").keys())

        assert (
            client.post(
                "/v1/chat/completions", json=_chat([_user("alpha")]), headers=AUTH
            ).status_code
            == 200
        )
        # Exercise the whole M12 surface in between chat turns.
        assert client.get("/admin").status_code == 200
        assert client.get("/admin/summary").status_code == 200
        assert client.get("/admin/sessions").status_code == 200
        assert client.get("/admin/settings").status_code == 200
        assert client.post("/admin/accounts/acct-2/disable").status_code == 200
        assert client.post("/admin/accounts/acct-2/reset").status_code == 200

        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("alpha"), _assistant("A1"), _user("beta")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A2"

        # /health and the pre-M12 admin shapes are untouched.
        assert client.get("/health").json() == health_before
        assert set(_account_row(client, "acct-1").keys()) == account_row_keys
        metrics = client.get("/admin/metrics").json()
        for key in (
            "requests",
            "request_seconds",
            "backend_attempts",
            "backend_failures",
            "transport_retries",
            "session_failovers",
            "tool_turns",
            "uptime_seconds",
        ):
            assert key in metrics
