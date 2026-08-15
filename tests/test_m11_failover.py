"""M11 tests (ADR-038): bounded in-request session failover.

Offline suite proving the M11 exit criterion — "a simulated account
failure can continue reconstructable context safely":

* a FINAL consequence-bearing pre-byte failure (401- or 429-class) on
  one account establishes EXACTLY ONE failover session on another
  usable account, REHYDRATES the request's full canonical history
  (tool history/IDs preserved by construction), and re-runs the same
  turn — the failing request succeeds transparently;
* the ``session_failovers`` metrics marker counts ESTABLISHED
  failovers only;
* failover never changes an error it cannot absorb: no usable target,
  establishment failure, non-consequence categories, and single-account
  deployments all surface the ORIGINAL error byte-identical;
* failover never chains (exactly one), and mid-stream failures never
  fail over (committed-stream rule);
* a failed re-run surfaces ITSELF with the failover account's
  consequence recorded.

Everything runs offline against scripted ``FakeBackend`` instances.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.accounts import (
    ACCOUNT_COOLDOWN,
    ACCOUNT_HEALTHY,
    ACCOUNT_INVALID,
    AccountRecord,
    AccountRouter,
)
from app.backends.errors import BackendErrorCategory, BackendFailure
from app.backends.events import MessageFinished, MessageStarted, TextDelta
from app.backends.fake import FakeBackend
from app.config import GatewaySettings
from app.conversation import Conversation, ConversationStore
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


def _failure(category: BackendErrorCategory, message: str = "scripted") -> BackendFailure:
    return BackendFailure(category=category, message=message)


def _chat(messages: list, **overrides) -> dict:
    body: dict = {"model": MODEL, "messages": messages, "stream": False}
    body.update(overrides)
    return body


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


def _accounts(client: TestClient) -> list[dict]:
    response = client.get("/admin/accounts")
    assert response.status_code == 200
    return response.json()["accounts"]


def _failovers(client: TestClient) -> int:
    return client.app.state.metrics.snapshot()["session_failovers"]


def _conversation_starting_with(
    store: ConversationStore, content: str
) -> Conversation:
    for conversation in store.conversations():
        if conversation.messages and conversation.messages[0].content == content:
            return conversation
    raise AssertionError(f"no conversation starts with {content!r}")


def _envelope_text(name: str, arguments: str) -> str:
    return (
        f"{TOOL_CALL_START_SENTINEL}\n"
        f'{{"name":"{name}","arguments":{arguments}}}\n'
        f"{TOOL_CALL_END_SENTINEL}"
    )


def _envelope_turn(name: str, arguments: str) -> list:
    return [
        MessageStarted(),
        TextDelta(_envelope_text(name, arguments)),
        MessageFinished("stop"),
    ]


# ---------------------------------------------------------------------------
# Transparent failover on turn failures (non-streaming plain path)
# ---------------------------------------------------------------------------


class TestNonStreamFailover:
    def test_401_class_turn_failure_fails_over_transparently(self) -> None:
        b1 = FakeBackend(
            turns=[_turn("A1"), [_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        b2 = FakeBackend(turns=[_turn("R1")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        # The failing request succeeds transparently on acct-2.
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "R1"
        assert _failovers(client) == 1
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_INVALID
        assert rows[1]["state"] == ACCOUNT_HEALTHY
        # Rebound to the failover account: fresh session, full history,
        # parent reset. Never migrated back.
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-2"
        assert conversation.backend_session_id == "fake-session-1"
        rerun = b2.turn_calls[0]
        assert rerun.session_id == "fake-session-1"
        assert rerun.parent_message_id is None
        assert "one" in rerun.prompt and "A1" in rerun.prompt
        assert "two" in rerun.prompt

    def test_failover_rehydrates_tool_history_and_ids(self) -> None:
        # Exit criterion: tool history/IDs survive the failover
        # semantically — the rehydrated prompt is the identical rebuild
        # compilation of the FULL canonical history.
        b1 = FakeBackend(
            turns=[_turn("A1"), [_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        b2 = FakeBackend(turns=[_turn("R2")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        messages = [
            _user("one"),
            _assistant("A1"),
            _assistant_tool_call(
                "call_x", "read_file", '{"file_path": "a.py"}'
            ),
            _tool_result("call_x", "file body"),
            _user("two"),
        ]
        response = client.post(
            "/v1/chat/completions", json=_chat(messages), headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "R2"
        assert _failovers(client) == 1
        prompt = b2.turn_calls[0].prompt
        # Historical assistant tool call re-rendered as the envelope
        # block (ADR-034), arguments byte-preserved (canonical form is
        # compact JSON).
        assert TOOL_CALL_START_SENTINEL in prompt
        assert '"name":"read_file"' in prompt
        assert '{"file_path":"a.py"}' in prompt
        # Tool result block with the ORIGINAL id, and the tool name
        # resolved through the tool-call id index (not "unknown").
        assert "[tool result]" in prompt
        assert "id: call_x" in prompt
        assert "tool: read_file" in prompt
        assert "file body" in prompt

    def test_429_class_turn_failure_fails_over_and_cools_source(self) -> None:
        # M9 budget first: RATE_LIMITED is retryable → 1 + 2 attempts
        # (one scripted turn per transport attempt), THEN the bounded
        # failover.
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
                [_failure(BackendErrorCategory.RATE_LIMITED)],
            ]
        )
        b2 = FakeBackend(turns=[_turn("R1")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        response = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "R1"
        assert _failovers(client) == 1
        assert len(b1.turn_calls) == 4  # seed + full retry budget
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_COOLDOWN
        assert rows[0]["cooldown_remaining_seconds"] > 0
        assert rows[1]["state"] == ACCOUNT_HEALTHY
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-2"


# ---------------------------------------------------------------------------
# Failover on session-creation failures (both _prepare_turn branches)
# ---------------------------------------------------------------------------


class TestSessionCreationFailover:
    def test_final_session_creation_failure_fails_over_before_any_turn(
        self,
    ) -> None:
        b1 = FakeBackend(
            turns=[_turn("never")],
            create_failures=[_failure(BackendErrorCategory.AUTH_INVALID)],
        )
        b2 = FakeBackend(turns=[_turn("R1")])
        client, router, store = _multi_client([b1, b2])

        response = client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "R1"
        assert _failovers(client) == 1
        assert _accounts(client)[0]["state"] == ACCOUNT_INVALID
        assert len(b1.turn_calls) == 0  # no turn ever ran on acct-1
        assert len(b1.sessions_created) == 0
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-2"
        assert conversation.backend_session_id == "fake-session-1"

    def test_failover_establishment_failure_surfaces_original_error(
        self,
    ) -> None:
        # The failover target's session creation fails FINALLY too →
        # the ORIGINAL failure surfaces byte-identical (best-effort
        # transparency), and the target account takes its consequence.
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [_failure(BackendErrorCategory.AUTH_INVALID, "first expired")],
            ]
        )
        b2 = FakeBackend(
            turns=[_turn("never")],
            create_failures=[
                _failure(BackendErrorCategory.AUTH_INVALID, "second expired")
            ],
        )
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 502
        error = failed.json()["error"]
        assert error["code"] == "AUTH_INVALID"
        assert error["message"] == "first expired"  # original, unchanged
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_INVALID
        assert rows[1]["state"] == ACCOUNT_INVALID  # establishment consequence
        assert _failovers(client) == 0  # never ESTABLISHED
        assert len(b2.turn_calls) == 0
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_session_id is None


# ---------------------------------------------------------------------------
# Eligibility + bounds (what NEVER fails over)
# ---------------------------------------------------------------------------


class TestEligibilityAndBounds:
    def test_non_consequence_category_never_fails_over(self) -> None:
        # Network/5xx/protocol failures are fleet-wide on one upstream:
        # failing over would only double the load on the same outage.
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [_failure(BackendErrorCategory.INTERNAL, "boom")],
            ]
        )
        b2 = FakeBackend(turns=[_turn("never")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "INTERNAL"
        assert _failovers(client) == 0
        assert len(b2.turn_calls) == 0
        assert len(b2.sessions_created) == 0
        # INTERNAL carries no account state consequence.
        assert _accounts(client)[0]["state"] == ACCOUNT_HEALTHY

    def test_single_account_deployment_error_surfaces_unchanged(self) -> None:
        # No failover target → byte-for-byte the pre-M11 behavior.
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [_failure(BackendErrorCategory.AUTH_INVALID, "expired")],
            ]
        )
        client, router, store = _multi_client([b1])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 502
        error = failed.json()["error"]
        assert error["code"] == "AUTH_INVALID"
        assert error["message"] == "expired"
        assert _failovers(client) == 0

    def test_failover_never_chains(self) -> None:
        # Exactly ONE failover: when the re-run fails too, the error
        # surfaces — no second hop to a still-healthy third account.
        b1 = FakeBackend(
            turns=[_turn("A1"), [_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        b2 = FakeBackend(
            turns=[[_failure(BackendErrorCategory.AUTH_INVALID, "second")]]
        )
        b3 = FakeBackend(turns=[_turn("never")])
        client, router, store = _multi_client([b1, b2, b3])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 502
        assert failed.json()["error"]["message"] == "second"  # re-run's own
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_INVALID
        assert rows[1]["state"] == ACCOUNT_INVALID
        assert rows[2]["state"] == ACCOUNT_HEALTHY  # never hopped further
        assert _failovers(client) == 1
        assert len(b3.turn_calls) == 0
        assert len(b3.sessions_created) == 0
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_session_id is None

    def test_rerun_failure_surfaces_itself_with_target_consequence(
        self,
    ) -> None:
        # Error policy: the re-run's failure (not the original) is the
        # latest truth, and the failover account takes its consequence.
        b1 = FakeBackend(
            turns=[
                _turn("A1"),
                [_failure(BackendErrorCategory.AUTH_INVALID, "first")],
            ]
        )
        b2 = FakeBackend(
            turns=[
                [_failure(BackendErrorCategory.RATE_LIMITED, "second")],
                [_failure(BackendErrorCategory.RATE_LIMITED, "second")],
                [_failure(BackendErrorCategory.RATE_LIMITED, "second")],
            ]
        )
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        failed = client.post(
            "/v1/chat/completions",
            json=_chat([_user("one"), _assistant("A1"), _user("two")]),
            headers=AUTH,
        )
        assert failed.status_code == 429
        error = failed.json()["error"]
        assert error["code"] == "RATE_LIMITED"
        assert error["message"] == "second"
        assert _failovers(client) == 1  # established before the re-run
        rows = _accounts(client)
        assert rows[0]["state"] == ACCOUNT_INVALID
        assert rows[1]["state"] == ACCOUNT_COOLDOWN


# ---------------------------------------------------------------------------
# Streaming paths: priming fails over, mid-stream never does
# ---------------------------------------------------------------------------


class TestStreamingFailover:
    def test_stream_priming_failure_fails_over(self) -> None:
        b1 = FakeBackend(
            turns=[_turn("A1"), [_failure(BackendErrorCategory.AUTH_INVALID)]]
        )
        b2 = FakeBackend(turns=[_turn("R1")])
        client, router, store = _multi_client([b1, b2])

        client.post(
            "/v1/chat/completions", json=_chat([_user("one")]), headers=AUTH
        )
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_chat(
                [_user("one"), _assistant("A1"), _user("two")], stream=True
            ),
            headers=AUTH,
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line.strip()]
        assert lines[-1] == "data: [DONE]"
        content = "".join(
            chunk["choices"][0]["delta"].get("content", "")
            for chunk in (
                json.loads(line[len("data: ") :])
                for line in lines[:-1]
            )
            if chunk.get("choices")
        )
        assert content == "R1"
        assert _failovers(client) == 1
        conversation = _conversation_starting_with(store, "one")
        assert conversation.backend_account_id == "acct-2"

    def test_mid_stream_failure_never_fails_over(self) -> None:
        # Committed-stream rule: after HTTP 200 + first byte, failures
        # become an in-stream error envelope — never a failover.
        failure = BackendFailure(
            category=BackendErrorCategory.AUTH_INVALID, message="mid stream"
        )
        b1 = FakeBackend(
            turns=[[MessageStarted(), TextDelta("par"), failure]]
        )
        b2 = FakeBackend(turns=[_turn("never")])
        client, router, store = _multi_client([b1, b2])

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_chat([_user("one")], stream=True),
            headers=AUTH,
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line.strip()]
        assert "data: [DONE]" not in lines
        last = json.loads(lines[-1][len("data: ") :])
        assert last["error"]["code"] == "AUTH_INVALID"
        assert _failovers(client) == 0
        # The failed account still took its consequence.
        assert _accounts(client)[0]["state"] == ACCOUNT_INVALID
        assert len(b2.turn_calls) == 0
        assert len(b2.sessions_created) == 0


# ---------------------------------------------------------------------------
# Buffered tool path (both response modes)
# ---------------------------------------------------------------------------


class TestBufferedToolFailover:
    def test_non_stream_tool_turn_fails_over_and_keeps_tool_semantics(
        self,
    ) -> None:
        b1 = FakeBackend(turns=[[_failure(BackendErrorCategory.AUTH_INVALID)]])
        b2 = FakeBackend(
            turns=[_envelope_turn("read_file", '{"file_path":"src/main.py"}')]
        )
        client, router, store = _multi_client([b1, b2])

        response = client.post(
            "/v1/chat/completions",
            json=_chat(
                [_user("Read src/main.py")],
                tools=[READ_FILE_TOOL],
                tool_choice="required",
            ),
            headers=AUTH,
        )
        assert response.status_code == 200
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        call = choice["message"]["tool_calls"][0]
        assert call["function"]["name"] == "read_file"
        assert json.loads(call["function"]["arguments"]) == {
            "file_path": "src/main.py"
        }
        assert _failovers(client) == 1
        assert _accounts(client)[0]["state"] == ACCOUNT_INVALID
        conversation = _conversation_starting_with(store, "Read src/main.py")
        assert conversation.backend_account_id == "acct-2"
        # The rehydrated prompt carried the tool instructions.
        assert TOOL_CALL_START_SENTINEL in b2.turn_calls[0].prompt

    def test_stream_tool_turn_fails_over(self) -> None:
        b1 = FakeBackend(turns=[[_failure(BackendErrorCategory.AUTH_INVALID)]])
        b2 = FakeBackend(
            turns=[_envelope_turn("read_file", '{"file_path":"src/main.py"}')]
        )
        client, router, store = _multi_client([b1, b2])

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_chat(
                [_user("Read src/main.py")],
                stream=True,
                tools=[READ_FILE_TOOL],
                tool_choice="required",
            ),
            headers=AUTH,
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line.strip()]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(line[len("data: ") :]) for line in lines[:-1]]
        tool_chunks = [
            chunk
            for chunk in chunks
            if chunk["choices"][0]["delta"].get("tool_calls")
        ]
        assert tool_chunks  # the envelope became standard tool_calls deltas
        assert _failovers(client) == 1
        conversation = _conversation_starting_with(store, "Read src/main.py")
        assert conversation.backend_account_id == "acct-2"
