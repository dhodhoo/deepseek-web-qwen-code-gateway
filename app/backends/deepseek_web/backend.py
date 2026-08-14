"""DeepSeekWebBackend — M0 spike adapter around vendored deepseek4free.

This module is the ONLY place in the application that touches the private
DeepSeek Web API client. Everything upstream-specific (endpoints, headers,
PoW, cookies, Cloudflare quirks, SSE framing) stays behind this boundary, as
required by AGENTS.md.

M0 scope (per 00_MASTER_PROMPT.md):

* client initialization
* session creation
* one prompt, streamed
* normalized events + finish behavior observation
* upstream exception normalization

Stable backend interface work (formal Protocol, FakeBackend, config boundary)
is M1 and intentionally not anticipated here beyond keeping the surface small.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator

from ..errors import BackendFailure
from ..events import BackendEvent
from . import _vendor  # noqa: F401  (ensures vendored dsk is importable)
from .normalize import chunk_dict_to_events, classify_upstream_exception

__all__ = ["DeepSeekWebBackend"]

logger = logging.getLogger(__name__)


class DeepSeekWebBackend:
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

    # ------------------------------------------------------------------ info

    def health_check(self) -> dict[str, Any]:
        """Local (no-network) health information. Secrets are never included."""
        return {
            "type": self.backend_type,
            "client_ready": True,
            "cookies_loaded": bool(getattr(self._api, "cookies", {})),
        }

    # -------------------------------------------------------------- session

    def create_session(self) -> str:
        """Create a DeepSeek chat session and return its id.

        Raises :class:`app.backends.errors.BackendFailure` on any upstream
        problem, normalized into the error taxonomy.
        """
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
        return session_id

    # --------------------------------------------------------------- stream

    def stream_turn(
        self,
        chat_session_id: str,
        prompt: str,
        *,
        parent_message_id: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
        raw_sink: list[bytes] | None = None,
    ) -> Iterator[BackendEvent]:
        """Run one prompt turn and yield normalized events.

        ``raw_sink``: when a list is provided, every raw SSE line observed by
        the vendored client is appended to it (bytes, without trailing
        newline). The probe uses this to build sanitized fixtures; the
        application itself never persists raw upstream bytes.

        Upstream failures are raised as
        :class:`app.backends.errors.BackendFailure`.
        """
        api = self._api

        # Raw capture seam: wrap the instance's chunk parser without
        # modifying vendored source. The previous __dict__ state (present or
        # absent instance attribute) is restored exactly in finally.
        _sentinel = object()
        previous_parse_attr = api.__dict__.get("_parse_chunk", _sentinel)
        original_parse = api._parse_chunk
        if raw_sink is not None:
            def _capturing_parse(chunk: bytes):  # type: ignore[no-untyped-def]
                raw_sink.append(chunk)
                return original_parse(chunk)

            api._parse_chunk = _capturing_parse  # type: ignore[method-assign]

        try:
            try:
                chunks = api.chat_completion(
                    chat_session_id,
                    prompt,
                    parent_message_id=parent_message_id,
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
