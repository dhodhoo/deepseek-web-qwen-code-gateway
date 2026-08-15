"""M10 tests (ADR-037): the multi-account router.

Three layers, mirroring the milestone's exit criterion ("new
conversations avoid unhealthy accounts; healthy existing sessions stay
sticky"):

* router unit tests — registry validation, least-active/unused-first
  selection, lazy cooldown expiry, 401/429 consequences, sticky
  eligibility, operator lifecycle, masked summary (injectable clock,
  no HTTP);
* config boundary — ``DSQG_ACCOUNT_TOKENS`` parsing, mutual exclusion,
  fail-closed validation, secret hygiene;
* public-API integration — sticky-under-pressure routing, cooldown
  blocking NEW conversations only, final 401 → invalid + rebuild on
  another account, final 429 → cooldown + bound rebuild, deterministic
  all-unusable mapping, and the masked ``GET /admin/accounts``.

Everything runs offline against scripted ``FakeBackend`` instances.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.accounts import (
    ACCOUNT_COOLDOWN,
    ACCOUNT_DISABLED,
    ACCOUNT_HEALTHY,
    ACCOUNT_INVALID,
    AccountRecord,
    AccountRouter,
)
from app.backends.deepseek_web import DeepSeekWebBackend
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend
from app.config import ConfigError, GatewaySettings, build_router
from app.conversation import CanonicalMessage, Conversation, ConversationStore
from app.server import create_app

AUTH = {"Authorization": "Bearer test-key"}
MODEL = "deepseek-web"

TOKEN_A = "test-token-a"
TOKEN_B = "test-token-b"

ROW_KEYS = {
    "id",
    "label",
    "enabled",
    "state",
    "cooldown_remaining_seconds",
    "consecutive_failures",
    "active_conversations",
    "last_used_at",
}


class _Clock:
    """Injectable clock for deterministic router-time tests."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Helpers — router unit layer
# ---------------------------------------------------------------------------


def _settings(**overrides) -> GatewaySettings:
    base: dict = {"backend_type": "fake", "gateway_api_key": SecretStr("test-key")}
    base.update(overrides)
    return GatewaySettings(**base)


def _account(account_id: str) -> AccountRecord:
    return AccountRecord(
        id=account_id, label=f"Label {account_id}", backend=FakeBackend()
    )


def _router(
    count: int = 1,
    *,
    cooldown: float = 300.0,
    clock: _Clock | None = None,
) -> tuple[AccountRouter, list[FakeBackend], _Clock]:
    clock = clock or _Clock()
    backends = [FakeBackend() for _ in range(count)]
    router = AccountRouter(
        [
            AccountRecord(
                id=f"acct-{index}", label=f"Account {index}", backend=backend
            )
            for index, backend in enumerate(backends, start=1)
        ],
        cooldown_seconds=cooldown,
        clock=clock,
    )
    return router, backends, clock


def _seed_conversation(
    store: ConversationStore,
    conversation_id: str,
    account_id: str,
    *,
    session_id: str | None = "sess-x",
) -> Conversation:
    """Put a bound conversation into the store (selection/summary input)."""
    return store.put(
        Conversation(
            conversation_id=conversation_id,
            backend_type="fake",
            created_at=0.0,
            updated_at=0.0,
            messages=[CanonicalMessage(role="user", content="hi")],
            backend_account_id=account_id,
            backend_session_id=session_id,
            backend_parent_message_id=None,
        )
    )


# ---------------------------------------------------------------------------
# Helpers — config layer
# ---------------------------------------------------------------------------


def _env(**overrides: str) -> dict[str, str]:
    base = {"DSQG_ACCOUNT_TOKENS": f"{TOKEN_A},{TOKEN_B}"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Helpers — public API layer
# ---------------------------------------------------------------------------


def _turn(text: str) -> list:
    return [MessageStarted(), TextDelta(text), MessageFinished("stop")]


def _failure(category: BackendErrorCategory) -> BackendFailure:
    return BackendFailure(category=category, message="scripted failure")


def _chat(messages: list) -> dict:
    return {"model": MODEL, "messages": messages, "stream": False}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


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


def _accounts(client: TestClient) -> list[dict]:
    response = client.get("/admin/accounts")
    assert response.status_code == 200
    return response.json()["accounts"]


def _conversation_starting_with(
    store: ConversationStore, content: str
) -> Conversation:
    for conversation in store.conversations():
        if conversation.messages and conversation.messages[0].content == content:
            return conversation
    raise AssertionError(f"no conversation starts with {content!r}")


# ---------------------------------------------------------------------------
# Router construction + registry
# ---------------------------------------------------------------------------


class TestRouterConstruction:
    def test_requires_at_least_one_account(self) -> None:
        with pytest.raises(ValueError):
            AccountRouter([])

    def test_requires_unique_ids(self) -> None:
        with pytest.raises(ValueError):
            AccountRouter([_account("a"), _account("a")])

    def test_requires_uniform_backend_type(self) -> None:
        class _OtherBackend(FakeBackend):
            backend_type = "other"

        with pytest.raises(ValueError):
            AccountRouter(
                [
                    _account("a"),
                    AccountRecord(
                        id="b", label="b", backend=_OtherBackend()
                    ),
                ]
            )

    def test_requires_positive_cooldown_seconds(self) -> None:
        with pytest.raises(ValueError):
            AccountRouter([_account("a")], cooldown_seconds=0)

    def test_single_wraps_backend_with_default_id(self) -> None:
        backend = FakeBackend()
        router = AccountRouter.single(backend)
        assert [account.id for account in router.accounts] == ["default"]
        assert router.default_account.backend is backend
        assert router.backend_type == "fake"

    def test_stamps_timestamps_and_registry_lookups(self) -> None:
        clock = _Clock()
        router, _, _ = _router(2, clock=clock)
        first = router.accounts[0]
        assert first.created_at == clock.now
        assert first.updated_at == clock.now
        assert router.get("acct-1") is first
        assert router.get("missing") is None
        assert router.get(None) is None
        assert [account.id for account in router.accounts] == [
            "acct-1",
            "acct-2",
        ]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class TestSelection:
    def test_unused_first_then_least_active_then_lru(self) -> None:
        router, _, clock = _router(2)
        store = ConversationStore()
        assert router.select_for_new_conversation(store).id == "acct-1"
        clock.advance(1)
        # Unused accounts sort first (last_used_at None → 0).
        assert router.select_for_new_conversation(store).id == "acct-2"
        _seed_conversation(store, "conv-b", "acct-2")
        clock.advance(1)
        # Least active wins (0 < 1).
        assert router.select_for_new_conversation(store).id == "acct-1"
        _seed_conversation(store, "conv-a", "acct-1")
        clock.advance(1)
        # Active tied 1:1 → least recently used wins.
        assert router.select_for_new_conversation(store).id == "acct-2"

    def test_select_stamps_last_used(self) -> None:
        router, _, clock = _router(1)
        account = router.select_for_new_conversation(ConversationStore())
        assert account.last_used_at == clock.now

    def test_expired_cooldown_is_lazily_promoted(self) -> None:
        router, _, clock = _router(1, cooldown=100.0)
        store = ConversationStore()
        router.record_failure(
            "acct-1", BackendErrorCategory.RATE_LIMITED, store
        )
        with pytest.raises(BackendFailure) as excinfo:
            router.select_for_new_conversation(store)
        assert excinfo.value.category is BackendErrorCategory.RATE_LIMITED
        clock.advance(101)
        account = router.select_for_new_conversation(store)
        assert account.id == "acct-1"
        assert account.health_status == ACCOUNT_HEALTHY
        assert account.cooldown_until is None

    def test_no_usable_rate_limited_while_any_cooling(self) -> None:
        router, _, _ = _router(2, cooldown=100.0)
        store = ConversationStore()
        router.record_failure(
            "acct-1", BackendErrorCategory.AUTH_INVALID, store
        )
        router.record_failure(
            "acct-2", BackendErrorCategory.RATE_LIMITED, store
        )
        with pytest.raises(BackendFailure) as excinfo:
            router.select_for_new_conversation(store)
        assert excinfo.value.category is BackendErrorCategory.RATE_LIMITED
        assert "No usable backend account" in str(excinfo.value)

    def test_no_usable_auth_invalid_when_none_cooling(self) -> None:
        router, _, _ = _router(2)
        store = ConversationStore()
        router.record_failure(
            "acct-1", BackendErrorCategory.AUTH_INVALID, store
        )
        router.set_enabled("acct-2", False)
        with pytest.raises(BackendFailure) as excinfo:
            router.select_for_new_conversation(store)
        assert excinfo.value.category is BackendErrorCategory.AUTH_INVALID


# ---------------------------------------------------------------------------
# Consequences (final failures only — M9 budget already absorbed retries)
# ---------------------------------------------------------------------------


class TestConsequences:
    def test_auth_invalid_marks_invalid_and_releases_bound_links(self) -> None:
        router, _, _ = _router(2)
        store = ConversationStore()
        one = _seed_conversation(store, "conv-1", "acct-1", session_id="s1")
        two = _seed_conversation(store, "conv-2", "acct-1", session_id="s2")
        three = _seed_conversation(store, "conv-3", "acct-2", session_id="s3")
        router.record_failure(
            "acct-1", BackendErrorCategory.AUTH_INVALID, store
        )
        account = router.get("acct-1")
        assert account.health_status == ACCOUNT_INVALID
        assert account.cooldown_until is None
        assert account.consecutive_failures == 1
        # Links released; the binding itself survives (rebuild marker).
        assert one.backend_session_id is None
        assert two.backend_session_id is None
        assert one.backend_account_id == "acct-1"
        # Other accounts' conversations untouched.
        assert three.backend_session_id == "s3"

    def test_rate_limited_enters_cooldown_and_keeps_links(self) -> None:
        clock = _Clock()
        router, _, _ = _router(1, cooldown=100.0, clock=clock)
        store = ConversationStore()
        conversation = _seed_conversation(
            store, "conv-1", "acct-1", session_id="s1"
        )
        router.record_failure(
            "acct-1", BackendErrorCategory.RATE_LIMITED, store
        )
        account = router.get("acct-1")
        assert account.health_status == ACCOUNT_COOLDOWN
        assert account.cooldown_until == clock.now + 100.0
        # Sticky sessions survive a rate-limit window.
        assert conversation.backend_session_id == "s1"

    def test_other_categories_only_bump_counter(self) -> None:
        router, _, _ = _router(1)
        store = ConversationStore()
        for _ in range(2):
            router.record_failure(
                "acct-1", BackendErrorCategory.UPSTREAM_NETWORK, store
            )
        account = router.get("acct-1")
        assert account.health_status == ACCOUNT_HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 2

    def test_success_clears_cooldown_and_counters(self) -> None:
        clock = _Clock()
        router, _, _ = _router(1, cooldown=100.0, clock=clock)
        router.record_failure("acct-1", BackendErrorCategory.RATE_LIMITED)
        router.record_success("acct-1")
        account = router.get("acct-1")
        assert account.health_status == ACCOUNT_HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0
        assert account.last_used_at == clock.now

    def test_unknown_account_ids_are_ignored(self) -> None:
        router, _, _ = _router(1)
        router.record_success("missing")
        router.record_failure("missing", BackendErrorCategory.RATE_LIMITED)
        assert router.get("acct-1").health_status == ACCOUNT_HEALTHY


# ---------------------------------------------------------------------------
# Sticky eligibility + operator lifecycle (methods only; M12 gets the UI)
# ---------------------------------------------------------------------------


class TestStickyAndLifecycle:
    def test_sticky_account_allows_healthy_and_cooldown(self) -> None:
        router, _, _ = _router(2)
        assert router.sticky_account("acct-1").id == "acct-1"
        router.record_failure("acct-2", BackendErrorCategory.RATE_LIMITED)
        assert router.sticky_account("acct-2").id == "acct-2"

    def test_sticky_account_rejects_dead_accounts(self) -> None:
        router, _, _ = _router(2)
        router.record_failure("acct-1", BackendErrorCategory.AUTH_INVALID)
        router.set_enabled("acct-2", False)
        assert router.sticky_account("acct-1") is None
        assert router.sticky_account("acct-2") is None
        assert router.sticky_account("missing") is None
        assert router.sticky_account(None) is None

    def test_disable_releases_links_and_blocks_selection(self) -> None:
        router, _, _ = _router(2)
        store = ConversationStore()
        conversation = _seed_conversation(
            store, "conv-1", "acct-1", session_id="s1"
        )
        router.set_enabled("acct-1", False, store)
        assert conversation.backend_session_id is None
        assert router.select_for_new_conversation(store).id == "acct-2"
        router.set_enabled("acct-1", True)
        assert router.sticky_account("acct-1").id == "acct-1"

    def test_reset_restores_account(self) -> None:
        router, _, _ = _router(1)
        router.record_failure("acct-1", BackendErrorCategory.AUTH_INVALID)
        router.set_enabled("acct-1", False)
        router.reset("acct-1")
        account = router.get("acct-1")
        assert account.enabled is True
        assert account.health_status == ACCOUNT_HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0
        assert (
            router.select_for_new_conversation(ConversationStore()).id
            == "acct-1"
        )

    def test_set_enabled_and_reset_require_known_id(self) -> None:
        router, _, _ = _router(1)
        with pytest.raises(KeyError):
            router.set_enabled("missing", False)
        with pytest.raises(KeyError):
            router.reset("missing")


# ---------------------------------------------------------------------------
# Masked admin summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_states_counters_and_masking(self) -> None:
        clock = _Clock()
        router, _, _ = _router(4, cooldown=300.0, clock=clock)
        store = ConversationStore()
        router.select_for_new_conversation(store)  # stamps acct-1 last_used
        _seed_conversation(store, "conv-1", "acct-1")
        _seed_conversation(store, "conv-2", "acct-1")
        router.record_failure(
            "acct-2", BackendErrorCategory.RATE_LIMITED, store
        )
        clock.advance(30)
        _seed_conversation(store, "conv-3", "acct-3")
        router.record_failure(
            "acct-3", BackendErrorCategory.AUTH_INVALID, store
        )
        router.set_enabled("acct-4", False, store)

        rows = router.summary(store)
        assert [row["id"] for row in rows] == [
            "acct-1",
            "acct-2",
            "acct-3",
            "acct-4",
        ]
        assert [row["state"] for row in rows] == [
            ACCOUNT_HEALTHY,
            ACCOUNT_COOLDOWN,
            ACCOUNT_INVALID,
            ACCOUNT_DISABLED,
        ]
        assert rows[1]["cooldown_remaining_seconds"] == 270.0
        # Active counts derive from the store, even for dead accounts
        # (binding survives link release until the next request rebinds).
        assert [row["active_conversations"] for row in rows] == [2, 0, 1, 0]
        assert rows[2]["consecutive_failures"] == 1
        assert rows[0]["last_used_at"] == 1000.0
        assert rows[3]["last_used_at"] is None
        for row in rows:
            assert set(row) == ROW_KEYS

    def test_summary_expired_cooldown_reports_healthy(self) -> None:
        clock = _Clock()
        router, _, _ = _router(1, cooldown=100.0, clock=clock)
        store = ConversationStore()
        router.record_failure(
            "acct-1", BackendErrorCategory.RATE_LIMITED, store
        )
        clock.advance(101)
        row = router.summary(store)[0]
        assert row["state"] == ACCOUNT_HEALTHY
        assert row["cooldown_remaining_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Config boundary: DSQG_ACCOUNT_TOKENS
# ---------------------------------------------------------------------------


class TestConfigMultiAccount:
    def test_account_tokens_parse_into_accounts(self) -> None:
        settings = GatewaySettings.from_env(_env())
        assert settings.backend_type == "deepseek_web"
        assert settings.deepseek_accounts is not None
        assert [
            account.auth_token.get_secret_value()
            for account in settings.deepseek_accounts
        ] == [TOKEN_A, TOKEN_B]
        # deepseek_web mirrors the FIRST account (compat surface).
        assert settings.deepseek_web is settings.deepseek_accounts[0]
        assert settings.account_cooldown_seconds == 300.0

    def test_cooldown_seconds_env_parsed(self) -> None:
        settings = GatewaySettings.from_env(
            _env(DSQG_ACCOUNT_COOLDOWN_SECONDS="45.5")
        )
        assert settings.account_cooldown_seconds == 45.5

    def test_token_and_tokens_mutually_exclusive(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env(
                _env(DEEPSEEK_AUTH_TOKEN="single-account-token")
            )
        message = str(excinfo.value)
        assert "DEEPSEEK_AUTH_TOKEN" in message
        assert "DSQG_ACCOUNT_TOKENS" in message
        assert TOKEN_A not in message

    def test_empty_entry_rejected(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env(
                _env(DSQG_ACCOUNT_TOKENS=f"{TOKEN_A},,{TOKEN_B}")
            )
        assert "empty entry" in str(excinfo.value)
        with pytest.raises(ConfigError):
            GatewaySettings.from_env(
                _env(DSQG_ACCOUNT_TOKENS=f"{TOKEN_A},{TOKEN_B},")
            )

    def test_duplicate_tokens_rejected(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env(
                _env(DSQG_ACCOUNT_TOKENS=f"{TOKEN_A},{TOKEN_A}")
            )
        assert "duplicate" in str(excinfo.value)
        assert TOKEN_A not in str(excinfo.value)

    def test_cookies_incompatible_with_multi_account(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env(
                _env(DSQG_COOKIES_FILE="C:/tmp/cookies.json")
            )
        assert "DSQG_COOKIES_FILE" in str(excinfo.value)

    def test_cooldown_must_be_positive_number(self) -> None:
        for raw in ("0", "-1", "abc"):
            with pytest.raises(ConfigError):
                GatewaySettings.from_env(
                    _env(DSQG_ACCOUNT_COOLDOWN_SECONDS=raw)
                )

    def test_fake_backend_ignores_account_tokens(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "GATEWAY_BACKEND": "fake",
                "DSQG_ACCOUNT_TOKENS": f"{TOKEN_A},{TOKEN_B}",
            }
        )
        assert settings.backend_type == "fake"
        assert settings.deepseek_accounts is None

    def test_multi_account_secrets_masked_in_serialization(self) -> None:
        settings = GatewaySettings.from_env(_env())
        assert TOKEN_A not in repr(settings)
        assert TOKEN_A not in settings.model_dump_json()


# ---------------------------------------------------------------------------
# build_router wiring
# ---------------------------------------------------------------------------


class TestBuildRouter:
    def test_fake_backend_single_default_account(self) -> None:
        router = build_router(
            GatewaySettings.from_env({"GATEWAY_BACKEND": "fake"})
        )
        assert [account.id for account in router.accounts] == ["default"]
        assert isinstance(router.default_account.backend, FakeBackend)
        assert router.backend_type == "fake"

    def test_single_token_single_default_account(self) -> None:
        settings = GatewaySettings.from_env({"DEEPSEEK_AUTH_TOKEN": TOKEN_A})
        router = build_router(settings)
        assert [account.id for account in router.accounts] == ["default"]
        backend = router.default_account.backend
        # Construction is offline; no network happens here.
        assert isinstance(backend, DeepSeekWebBackend)
        assert backend.health_check().ready is True

    def test_multi_token_accounts_in_config_order(self) -> None:
        router = build_router(GatewaySettings.from_env(_env()))
        assert [account.id for account in router.accounts] == [
            "acct-1",
            "acct-2",
        ]
        assert [account.label for account in router.accounts] == [
            "DeepSeek account 1",
            "DeepSeek account 2",
        ]
        first, second = router.accounts
        assert isinstance(first.backend, DeepSeekWebBackend)
        assert isinstance(second.backend, DeepSeekWebBackend)
        # Per-account isolation: each account owns its own backend (own
        # vendored client + PoW solver + call gate, ADR-037 point 2).
        assert first.backend is not second.backend
        assert router.backend_type == "deepseek_web"

    def test_deepseek_without_settings_raises(self) -> None:
        with pytest.raises(ConfigError):
            build_router(GatewaySettings(backend_type="deepseek_web"))

    def test_unknown_backend_type_raises(self) -> None:
        with pytest.raises(ConfigError):
            build_router(GatewaySettings(backend_type="mystery"))


# ---------------------------------------------------------------------------
# Public API: routing, stickiness, consequences, admin surface
# ---------------------------------------------------------------------------


class TestApiRouting:
    def test_sticky_conversation_survives_balancing_pressure(self) -> None:
        # A (acct-1), B (acct-2), C (acct-1 via LRU tie-break) → acct-1
        # is the BUSIER account; continuing A must still stay on acct-1
        # (never round-robin per turn — ARCHITECTURE.md).
        b1 = FakeBackend(turns=[_turn("A1"), _turn("C1"), _turn("A2")])
        b2 = FakeBackend(turns=[_turn("B1")])
        client, router, store = _multi_client([b1, b2])

        assert (
            client.post(
                "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
            ).json()["choices"][0]["message"]["content"]
            == "A1"
        )
        assert (
            client.post(
                "/v1/chat/completions",
                json=_chat([_user("beta")]),
                headers=AUTH,
            ).json()["choices"][0]["message"]["content"]
            == "B1"
        )
        assert (
            client.post(
                "/v1/chat/completions",
                json=_chat([_user("gamma")]),
                headers=AUTH,
            ).json()["choices"][0]["message"]["content"]
            == "C1"
        )
        assert len(b1.sessions_created) == 2
        assert len(b2.sessions_created) == 1

        # Least-active selection WOULD pick acct-2 (1 active vs 2) — the
        # sticky binding must win instead.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A2"
        assert len(b2.sessions_created) == 1  # acct-2 untouched
        assert len(b2.turn_calls) == 1
        assert b1.turn_calls[-1].session_id == "fake-session-1"
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-1"

    def test_cooldown_blocks_new_conversations_but_not_sticky_sessions(
        self,
    ) -> None:
        b1 = FakeBackend(turns=[_turn("A1"), _turn("A2")])
        b2 = FakeBackend(turns=[_turn("B1")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        router.record_failure(
            "acct-1", BackendErrorCategory.RATE_LIMITED, store
        )

        # NEW conversation avoids the cooling account.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("beta")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "B1"
        assert len(b1.sessions_created) == 1  # no new session on acct-1
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_COOLDOWN

        # The EXISTING sticky session keeps working through the window.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "A2"
        assert len(b1.sessions_created) == 1  # session reused, not created
        assert b1.turn_calls[-1].session_id == "fake-session-1"
        # The committed turn counts as success → cooldown cleared.
        assert _accounts(client)[0]["state"] == ACCOUNT_HEALTHY

    def test_final_429_cools_account_and_rebuild_stays_bound(self) -> None:
        # M9 budget: RATE_LIMITED is retryable → 1 + 2 scripted attempts.
        # M11 (ADR-038) re-pin: acct-2 is DISABLED, so the failover has
        # no usable target — the failing request must surface the
        # ORIGINAL 429 byte-identical (failover is best-effort
        # transparency; it never changes an error it cannot absorb).
        rate_limited = _failure(BackendErrorCategory.RATE_LIMITED)
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [rate_limited],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                _turn("A2"),
            ]
        )
        b2 = FakeBackend(turns=[_turn("B1"), _turn("C1")])
        client, router, store = _multi_client(
            [b1, b2], settings=_settings(retry_backoff_seconds=0.0)
        )
        router.set_enabled("acct-2", False, store)

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )

        # Continuation of A on acct-1 exhausts the budget → final 429,
        # unchanged: no usable failover target exists.
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 429
        assert failed.json()["error"]["code"] == "RATE_LIMITED"
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_COOLDOWN
        assert rows[0]["cooldown_remaining_seconds"] > 0
        assert rows[0]["consecutive_failures"] == 1
        assert rows[1]["state"] == ACCOUNT_DISABLED
        # No failover was ESTABLISHED → the marker stays untouched.
        snapshot = client.app.state.metrics.snapshot()
        assert snapshot["session_failovers"] == 0
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_session_id is None
        assert conversation.backend_account_id == "acct-1"

        # New conversations have nowhere to go: acct-1 cools (cooldown
        # blocks NEW conversations) and acct-2 is disabled → the fleet
        # no-usable-account error.
        blocked = client.post(
            "/v1/chat/completions",
            json=_chat([_user("gamma")]),
            headers=AUTH,
        )
        assert blocked.status_code == 429
        body = blocked.json()["error"]
        assert body["code"] == "RATE_LIMITED"
        assert "No usable backend account" in body["message"]
        assert len(b1.sessions_created) == 1
        assert len(b2.sessions_created) == 0

        # The failed conversation REBUILDS on its bound account (cooldown
        # never disqualifies a sticky-bound rebuild — ADR-037 refinement):
        # full-history prompt into a fresh acct-1 session.
        retry = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert retry.status_code == 200
        assert retry.json()["choices"][0]["message"]["content"] == "A2"
        assert len(b1.sessions_created) == 2
        rebuild_call = b1.turn_calls[-1]
        assert rebuild_call.session_id == "fake-session-2"
        assert "one" in rebuild_call.prompt and "two" in rebuild_call.prompt
        assert conversation.backend_account_id == "acct-1"
        assert conversation.backend_session_id == "fake-session-2"
        # Success cleared the cooldown window.
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_HEALTHY
        assert rows[0]["cooldown_remaining_seconds"] == 0.0

    def test_final_401_invalidates_and_rebuilds_elsewhere(self) -> None:
        # AUTH_INVALID is non-retryable → exactly ONE attempt.
        b1 = FakeBackend(
            turns=[_turn("A1"), [_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        b2 = FakeBackend(turns=[_turn("R1"), _turn("B1")])
        client, router, store = _multi_client(
            [b1, b2], settings=_settings(retry_backoff_seconds=0.0)
        )

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        # M11 (ADR-038) re-pin: this request no longer surfaces the 401.
        # The bounded in-request failover records acct-1's consequence
        # (invalid), establishes ONE session on acct-2, rehydrates the
        # FULL canonical history there, and re-runs the same turn — the
        # failing request succeeds transparently.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "R1"
        assert len(b1.turn_calls) == 2  # no retry for the 401-class
        assert _accounts(client)[0]["state"] == ACCOUNT_INVALID
        # Metrics marker: exactly one established failover.
        snapshot = client.app.state.metrics.snapshot()
        assert snapshot["session_failovers"] == 1
        # The conversation is bound to the failover account (never
        # migrated back), and the re-run got the FULL history on a
        # fresh session (parent_message_id reset).
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-2"
        assert conversation.backend_session_id == "fake-session-1"
        rebuild_call = b2.turn_calls[-1]
        assert rebuild_call.session_id == "fake-session-1"
        assert rebuild_call.parent_message_id is None
        assert "one" in rebuild_call.prompt and "two" in rebuild_call.prompt

        # New conversations also avoid the invalid account; the fleet is
        # still healthy overall.
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("beta")]),
            headers=AUTH,
        )
        assert response.json()["choices"][0]["message"]["content"] == "B1"
        assert len(b1.sessions_created) == 1
        assert len(b2.sessions_created) == 2
        assert client.get("/health").json()["ok"] is True

    def test_all_accounts_unusable_maps_429_or_502(self) -> None:
        # Any account still cooling → 429 (client backs off).
        b1 = FakeBackend()
        client, router, store = _multi_client([b1])
        router.record_failure(
            "acct-1", BackendErrorCategory.RATE_LIMITED, store
        )
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 429
        body = response.json()["error"]
        assert body["code"] == "RATE_LIMITED"
        assert "No usable backend account" in body["message"]
        assert len(b1.sessions_created) == 0  # never touched

        # None cooling (invalid/disabled only) → 502 (operator action).
        b2 = FakeBackend()
        client2, router2, store2 = _multi_client([b2])
        router2.record_failure(
            "acct-1", BackendErrorCategory.AUTH_INVALID, store2
        )
        response = client2.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"

        router2.reset("acct-1")
        router2.set_enabled("acct-1", False, store2)
        response = client2.post(
            "/v1/chat/completions", json=_chat([_user("two")]), headers=AUTH
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "AUTH_INVALID"

    def test_admin_accounts_payload_is_masked_and_unauthenticated(
        self,
    ) -> None:
        b1 = FakeBackend(turns=[_turn("A1")])
        b2 = FakeBackend()
        client, router, store = _multi_client([b1, b2])
        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )

        response = client.get("/admin/accounts")  # NO auth header
        assert response.status_code == 200
        rows = response.json()["accounts"]
        assert [row["id"] for row in rows] == ["acct-1", "acct-2"]
        for row in rows:
            assert set(row) == ROW_KEYS
        assert rows[0]["state"] == ACCOUNT_HEALTHY
        assert rows[0]["active_conversations"] == 1
        assert rows[0]["last_used_at"] is not None
        assert rows[1]["active_conversations"] == 0
        assert rows[1]["last_used_at"] is None

    def test_health_not_ready_when_all_accounts_invalid(self) -> None:
        b1 = FakeBackend(
            turns=[[_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        client, router, store = _multi_client(
            [b1], settings=_settings(retry_backoff_seconds=0.0)
        )
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 502
        health = client.get("/health").json()
        assert health["ok"] is False
        assert health["backend"]["status"] == "not_ready"

    def test_create_app_rejects_backend_and_router_together(self) -> None:
        with pytest.raises(ValueError):
            create_app(
                _settings(),
                backend=FakeBackend(),
                router=AccountRouter.single(FakeBackend()),
            )

    def test_create_app_backend_injection_keeps_single_default_account(
        self,
    ) -> None:
        backend = FakeBackend(turns=[_turn("A1")])
        store = ConversationStore()
        app = create_app(_settings(), backend, store)
        assert [account.id for account in app.state.router.accounts] == [
            "default"
        ]
        assert app.state.backend is backend
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 200
        assert store.conversations()[0].backend_account_id == "default"
