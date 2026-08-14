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

from app.backends.deepseek_web import DeepSeekWebBackend
from app.backends.errors import BackendFailure
from app.backends.events import MessageFinished, TextDelta

pytestmark = pytest.mark.live

TOKEN = os.environ.get("DEEPSEEK_AUTH_TOKEN", "").strip()

skip_no_token = pytest.mark.skipif(
    not TOKEN, reason="DEEPSEEK_AUTH_TOKEN not set; live tests skipped"
)


@skip_no_token
def test_live_session_and_simple_prompt() -> None:
    backend = DeepSeekWebBackend(TOKEN)

    session_id = backend.create_session()
    assert isinstance(session_id, str) and session_id

    events = list(
        backend.stream_turn(
            session_id, "Reply with exactly one word: OK", thinking_enabled=False
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
