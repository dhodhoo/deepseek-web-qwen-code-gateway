"""Live upstream smoke tests (pytest marker: ``live``).

Excluded from default runs by ``pyproject.toml`` (``addopts = -m 'not live'``).
Run explicitly with a valid credential:

    set DEEPSEEK_AUTH_TOKEN=<token>
    .venv\\Scripts\\python.exe -m pytest -m live -v

Never run in CI. Never print credentials.
"""

from __future__ import annotations

import os

import pytest

from app.backends import BackendSession
from app.backends.deepseek_web import DeepSeekWebBackend
from app.backends.errors import BackendFailure
from app.backends.events import BackendMessageId, MessageFinished, TextDelta

pytestmark = pytest.mark.live

TOKEN = os.environ.get("DEEPSEEK_AUTH_TOKEN", "").strip()

skip_no_token = pytest.mark.skipif(
    not TOKEN, reason="DEEPSEEK_AUTH_TOKEN not set; live tests skipped"
)


@skip_no_token
def test_live_session_and_simple_prompt() -> None:
    backend = DeepSeekWebBackend(TOKEN)

    session = backend.create_session()
    assert isinstance(session, BackendSession)
    assert session.session_id

    events = list(
        backend.stream_turn(
            session.session_id,
            "Reply with exactly one word: OK",
            thinking_enabled=False,
        )
    )
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    finishes = [e for e in events if isinstance(e, MessageFinished)]

    assert text.strip(), "expected non-empty assistant text"
    assert finishes, "expected a terminal MessageFinished event"
    assert finishes[-1].finish_reason == "stop"


@skip_no_token
def test_live_invalid_token_is_auth_invalid() -> None:
    backend = DeepSeekWebBackend("invalid-token-" + "x" * 16)
    with pytest.raises(BackendFailure) as excinfo:
        backend.create_session()
    assert excinfo.value.category.value == "AUTH_INVALID"


@skip_no_token
def test_live_multi_turn_threads_parent_message_id() -> None:
    """M4 acceptance probe (multi-turn acceptance was deferred from M0).

    Verifies live that upstream accepts a second turn on the SAME session,
    parented under the first turn's ``response_message_id`` (the threading
    id from the ``event: ready`` frame), and that the conversation context
    survives: the model must recall a word only given in turn one. If this
    fails, the delta+parent strategy of ADR-020 needs revisiting (fallback:
    full-history rebuild every turn).
    """
    backend = DeepSeekWebBackend(TOKEN)
    session = backend.create_session()

    first = list(
        backend.stream_turn(
            session.session_id,
            "Remember the word ALPHA. Reply with exactly one word: OK",
            thinking_enabled=False,
        )
    )
    first_ids = [event.id for event in first if isinstance(event, BackendMessageId)]
    assert first_ids, "the ready frame should expose a response_message_id"
    assert any(isinstance(event, MessageFinished) for event in first)

    second = list(
        backend.stream_turn(
            session.session_id,
            "What word did I ask you to remember? Reply with exactly that word.",
            parent_message_id=first_ids[-1],
            thinking_enabled=False,
        )
    )
    text = "".join(event.text for event in second if isinstance(event, TextDelta))
    finishes = [event for event in second if isinstance(event, MessageFinished)]

    assert text.strip(), "expected non-empty assistant text on turn two"
    assert finishes, "expected a terminal MessageFinished event on turn two"
    assert finishes[-1].finish_reason == "stop"
    assert "ALPHA" in text.upper(), (
        "upstream did not honor parent threading (word from turn one lost)"
    )
