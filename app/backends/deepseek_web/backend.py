"""DeepSeekWebBackend — adapter around vendored deepseek4free.

This module is the ONLY place in the application that touches the private
DeepSeek Web API client. Everything upstream-specific (endpoints, headers,
PoW, cookies, Cloudflare quirks, SSE framing) stays behind this boundary, as
required by AGENTS.md.

Originating milestones (per 00_MASTER_PROMPT.md):

* M0: client initialization, session creation, one streamed prompt,
  normalized events + finish behavior observation, upstream exception
  normalization.
* M1: explicit conformance to the stable :class:`app.backends.base.LLMBackend`
  interface (typed ``BackendSession``/``BackendHealth`` returns).

The ``raw_sink`` keyword of :meth:`DeepSeekWebBackend.stream_turn` is a
backend-specific extension used by the probe for sanitized fixture capture;
it is NOT part of the stable interface and nothing above the backend layer
may rely on it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Iterator

from ..base import BackendHealth, BackendSession, LLMBackend
from ..errors import BackendFailure
from ..events import BackendEvent
from . import _vendor  # noqa: F401  (ensures vendored dsk is importable)
from .normalize import chunk_dict_to_events, classify_upstream_exception
from .wire import WireSession

__all__ = ["DeepSeekWebBackend"]

logger = logging.getLogger(__name__)


class DeepSeekWebBackend(LLMBackend):
    """Thin, normalized wrapper over the vendored ``DeepSeekAPI`` client."""

    backend_type = "deepseek_web"

    def __init__(
        self,
        auth_token: str,
        cookies_file: str | Path | None = None,
    ) -> None:
        """Initialize the vendored client.

        ``cookies_file`` (optional) points at a JSON file shaped like
        ``{"cookies": {...}}`` (upstream format). When provided, its cookies
        replace whatever the vendored client loaded from its default
        location. Cookies are treated as secrets: never logged.
        """
        from dsk.api import DeepSeekAPI  # vendored

        try:
            self._api = DeepSeekAPI(auth_token)
        except Exception as exc:  # AuthenticationError, wasmtime failures...
            raise classify_upstream_exception(exc) from exc

        if cookies_file is not None:
            path = Path(cookies_file)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BackendFailure(
                    category=_client_bad_request(),
                    message=f"Could not read cookies file: {exc}",
                    cause=exc,
                ) from exc
            cookies = data.get("cookies") if isinstance(data, dict) else None
            if not isinstance(cookies, dict):
                raise BackendFailure(
                    category=_client_bad_request(),
                    message='Cookies file must contain a top-level "cookies" object',
                )
            self._api.cookies = cookies

        # The vendored client is NOT thread-safe, in two independent ways:
        #
        # 1. Its PoW solver (dsk/pow.py) shares ONE wasmtime
        #    Engine/Store/Instance across all requests. Concurrent solves
        #    corrupt the wasm stack and panic wasmtime's unwinder
        #    ("crates\\unwinder\\src\\stackwalk.rs", assertion about a
        #    contiguous sequence of Wasm frames), which ABORTS the whole
        #    gateway interpreter. Live evidence (post-M6): Qwen Code sends
        #    a side query and an agent turn in the same second; every such
        #    paired arrival crashed python.exe (local crash dumps captured
        #    at the exact request timestamps).
        # 2. stream_turn installs a per-turn parser attribute on the
        #    SHARED client instance; concurrent turns would overwrite each
        #    other's parser mid-stream.
        #
        # Serialize every backend call therefore (single-account backend,
        # ADR-005/ADR-027 — queued turns are far cheaper than a dead
        # gateway). A Semaphore(1) is used instead of a Lock because one
        # streamed turn is iterated by several threads over its lifetime
        # (priming, threadpool hops, aclose) and the gate must release on
        # whichever thread happens to finish it; threading.Lock would
        # raise on cross-thread release.
        self._call_gate = threading.Semaphore(1)

    # ------------------------------------------------------------------ info

    def health_check(self) -> BackendHealth:
        """Local (no-network) health information. Secrets are never included."""
        return BackendHealth(
            backend_type=self.backend_type,
            ready=True,
            details={"cookies_loaded": bool(getattr(self._api, "cookies", {}))},
        )

    # -------------------------------------------------------------- session

    def create_session(self) -> BackendSession:
        """Create a DeepSeek chat session.

        Raises :class:`app.backends.errors.BackendFailure` on any upstream
        problem, normalized into the error taxonomy. Serialized through the
        call gate (see ``__init__`` — the vendored client is not
        thread-safe).
        """
        with self._call_gate:
            try:
                session_id = self._api.create_chat_session()
            except Exception as exc:
                raise classify_upstream_exception(exc) from exc
            if not isinstance(session_id, str) or not session_id:
                raise BackendFailure(
                    category=_upstream_protocol(),
                    message="Session creation returned no usable session id",
                )
            logger.info("deepseek_web session created")
            return BackendSession(session_id=session_id)

    # --------------------------------------------------------------- stream

    def stream_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        parent_message_id: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        raw_sink: list[bytes] | None = None,
    ) -> Iterator[BackendEvent]:
        """Run one prompt turn and yield normalized events.

        ``raw_sink``: when a list is provided, every raw SSE line observed
        from upstream is appended to it (bytes, without trailing newline)
        before protocol adaptation. The probe uses this to build sanitized
        fixtures; the application itself never persists raw upstream bytes.

        Upstream failures are raised as
        :class:`app.backends.errors.BackendFailure`.

        Serialized through the call gate for the turn's WHOLE lifetime
        (see ``__init__`` — the vendored client is not thread-safe): the
        gate is held across yields and released by whichever thread
        finishes or closes the generator.
        """
        with self._call_gate:
            api = self._api

            # Wire-format fix (live-verified post-M6): DeepSeek Web's
            # /chat/completion deserializes ``parent_message_id`` as a JSON
            # NUMBER (u32) and rejects the string the vendored client would
            # otherwise serialize — "invalid type: string ..., expected u32",
            # HTTP 422 — which silently broke EVERY session-reuse delta turn
            # (ADR-020/M4) and forced the rebuild path on each retry. Numeric
            # ids are converted here at the adapter boundary; the stable
            # LLMBackend interface and the conversation store keep their
            # string representation.
            upstream_parent: str | int | None
            if isinstance(parent_message_id, str) and parent_message_id.isdigit():
                upstream_parent = int(parent_message_id)
            else:
                upstream_parent = parent_message_id

            # Protocol seam: the vendored _parse_chunk implements the *legacy*
            # (pre-2026) wire format, which DeepSeek Web no longer serves. M0
            # live probing verified the current protocol; we replace the parser
            # at runtime with wire.adapt_raw_line (optionally capturing raw
            # lines first). The previous __dict__ state is restored exactly in
            # finally, so the vendored object is left untouched.
            _sentinel = object()
            previous_parse_attr = api.__dict__.get("_parse_chunk", _sentinel)

            wire_session = WireSession()

            def _backend_parse(chunk: bytes):  # type: ignore[no-untyped-def]
                if raw_sink is not None:
                    raw_sink.append(chunk)
                return wire_session.adapt(chunk)

            api._parse_chunk = _backend_parse  # type: ignore[method-assign]

            try:
                try:
                    chunks = api.chat_completion(
                        session_id,
                        prompt,
                        parent_message_id=upstream_parent,
                        thinking_enabled=thinking_enabled,
                        search_enabled=search_enabled,
                    )
                    for chunk in chunks:
                        yield from chunk_dict_to_events(chunk)
                except Exception as exc:
                    raise classify_upstream_exception(exc) from exc
            finally:
                if previous_parse_attr is _sentinel:
                    api.__dict__.pop("_parse_chunk", None)
                else:
                    api.__dict__["_parse_chunk"] = previous_parse_attr


def _client_bad_request():
    from ..errors import BackendErrorCategory

    return BackendErrorCategory.CLIENT_BAD_REQUEST


def _upstream_protocol():
    from ..errors import BackendErrorCategory

    return BackendErrorCategory.UPSTREAM_PROTOCOL
