"""Adapter for the CURRENT DeepSeek Web streaming protocol (M0 live finding).

Live probing on 2026-08-14 (sanitized captures in
``tests/fixtures/deepseek_web/live/``) showed that DeepSeek Web no longer
streams OpenAI-style ``choices[].delta`` chunks — the format the vendored
deepseek4free parser expects. The current protocol is an event + JSON-patch
style stream with a **sticky path** compression::

    event: ready
    data: {"request_message_id": "...", "response_message_id": "...", "model_type": "default"}

    event: update_session
    data: {"updated_at": 1786711092.380996}

    data: {"v": {"response": {"message_id": "...", "parent_id": "...",
              "role": "ASSISTANT", "status": "WIP", "content": "", ...}}}

    data: {"p": "response/content", "o": "APPEND", "v": "OK"}
    data: {"p": "response/accumulated_token_usage", "o": "SET", "v": 39}
    data: {"p": "response/status", "v": "FINISHED"}

    event: finish
    data: {}
    event: title
    data: {"content": "OK"}
    event: close
    data: {"click_behavior": "none", "auto_resume": false}

Sticky-path rules (verified against the thinking-enabled capture):

* ``p`` (path) is set once, then omitted on subsequent ops: they apply to
  the most recent path. Example thinking stream::

      data: {"p": "response/thinking_content", "v": "1"}
      data: {"o": "APPEND", "v": "."}
      data: {"v": " The"}
      ...

* ``o`` (op) may also be omitted; for content/thinking paths the effective
  op is APPEND, for status the value itself decides.

Use one :class:`WireSession` per stream (``DeepSeekWebBackend.stream_turn``
does this). :func:`adapt_raw_line` is the stateless convenience wrapper
(one-shot session) for streams where every op carries an explicit ``p``.

Observed protocol facts recorded for later milestones:

* ``response_message_id`` from the ``ready`` event is the threading id: the
  next turn's ``parent_message_id`` should be the previous turn's
  ``response_message_id`` (M4).
* ``response/status`` becomes ``FINISHED`` on success; the adapter maps it
  to ``finish_reason='stop'`` so the vendored loop's terminal-break fires
  and downstream layers see one normalized terminal event.
* The initial ``{"v": {"response": {...}}}`` snapshot may already carry
  non-empty ``content`` / ``thinking_content`` (the first generated tokens
  can arrive there, before any APPEND op); the adapter emits them as the
  first delta chunk so nothing is dropped.
* Text arrives via APPEND ops on ``response/content``; thinking (when
  enabled) via APPEND ops on ``response/thinking_content``, followed by
  ``response/thinking_elapsed_secs`` SET bookkeeping.
"""

from __future__ import annotations

import json
from typing import Any

from .normalize import RawStreamParseError, SSE_DATA_PREFIX

__all__ = ["WireSession", "adapt_raw_line"]

_PATH_CONTENT = "response/content"
_PATH_THINKING = "response/thinking_content"
_PATH_STATUS = "response/status"
_STATUS_FINISHED = "FINISHED"
_OP_APPEND = "APPEND"
_DELTAS_PATHS = {_PATH_CONTENT, _PATH_THINKING}


class WireSession:
    """Stateful adapter for one DeepSeek Web stream.

    Tracks the sticky ``p`` (path) across ops. Feed every raw SSE line from
    ``iter_lines()`` to :meth:`adapt`; it returns backend chunk dicts (the
    contract consumed by
    :func:`app.backends.deepseek_web.normalize.chunk_dict_to_events`) or
    ``None`` for lines carrying no stream data.
    """

    def __init__(self) -> None:
        self._current_path: str | None = None

    # ------------------------------------------------------------------ api

    def adapt(self, line: bytes | str) -> dict[str, Any] | None:
        if isinstance(line, str):
            raw = line.encode("utf-8", errors="replace")
        else:
            raw = bytes(line)

        raw = raw.strip()
        if not raw.startswith(SSE_DATA_PREFIX):
            # "event: ..." markers, comments, blank keepalives.
            return None

        body = raw[len(SSE_DATA_PREFIX):]
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RawStreamParseError(f"invalid JSON in SSE data line: {exc}") from exc

        if not isinstance(payload, dict):
            return None

        # -- ready event: message ids for threading -------------------------
        if "response_message_id" in payload:
            chunk: dict[str, Any] = {
                "content": "",
                "type": "",
                "finish_reason": None,
                "response_message_id": str(payload.get("response_message_id")),
            }
            request_id = payload.get("request_message_id")
            if request_id:
                chunk["request_message_id"] = str(request_id)
            return chunk

        # -- initial snapshot: {"v": {"response": {...}}} -------------------
        v = payload.get("v")
        if isinstance(v, dict):
            response = v.get("response")
            if isinstance(response, dict):
                chunk = {"content": "", "type": "", "finish_reason": None}
                message_id = response.get("message_id")
                if message_id:
                    chunk["response_message_id"] = str(message_id)
                # The snapshot can already carry generated text (live
                # evidence, post-M6: ``"content": "The"`` arrived here
                # BEFORE the first APPEND op). Dropping it silently loses
                # the first chunk of the answer — emit it. ``content``
                # wins over ``thinking_content`` when both are present;
                # the one-chunk-per-line contract cannot carry both.
                content = response.get("content")
                thinking = response.get("thinking_content")
                if isinstance(content, str) and content:
                    chunk["content"] = content
                    chunk["type"] = "text"
                elif isinstance(thinking, str) and thinking:
                    chunk["content"] = thinking
                    chunk["type"] = "thinking"
                return chunk
            return None

        # -- patch ops with sticky path --------------------------------------
        p = payload.get("p")
        if isinstance(p, str) and p:
            self._current_path = p
        path = self._current_path
        if not isinstance(path, str):
            return None

        value = payload.get("v")
        op = payload.get("o")

        if path == _PATH_CONTENT:
            if self._is_append(op) and value:
                return {"content": str(value), "type": "text", "finish_reason": None}
            return None
        if path == _PATH_THINKING:
            if self._is_append(op) and value:
                return {"content": str(value), "type": "thinking", "finish_reason": None}
            return None
        if path == _PATH_STATUS:
            if value == _STATUS_FINISHED:
                return {"content": "", "type": "", "finish_reason": "stop"}
            # WIP and any other status: not terminal, not an error in M0.
            return None

        # Any other path (token usage, tips, search state, elapsed-secs ...):
        # bookkeeping only.
        return None

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _is_append(op: Any) -> bool:
        """APPEND is explicit (``o: APPEND``) or implicit (``o`` omitted)."""
        return op is None or op == _OP_APPEND


def adapt_raw_line(line: bytes | str) -> dict[str, Any] | None:
    """Stateless convenience adapter (fresh session per line).

    Suitable for streams where every op carries an explicit ``p``; for real
    sticky-path streams use :class:`WireSession`.
    """
    return WireSession().adapt(line)
