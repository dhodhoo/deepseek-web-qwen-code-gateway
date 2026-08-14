"""Sanitization of captured upstream data before it becomes a fixture.

Security policy (docs/SECURITY.md): never persist tokens, cookies, account
identifiers, or personal data. The probe captures raw SSE lines from a canned
prompt; before anything is written into ``tests/fixtures`` it passes through
:func:`sanitize_raw_sse_lines`.

Rules:

* Every JSON string value under a key that is an identifier (``id`` or any
  ``*_id``) is replaced by a deterministic placeholder ``SANITIZED-ID-<n>``.
  The same original value always maps to the same placeholder within one
  capture, so ID relationships (e.g. parent message threading) stay testable
  without revealing real identifiers.
* Keys in :data:`REMOVED_KEYS` (account/user/personal/credential material)
  are removed entirely.
* Non-JSON lines pass through unchanged (keep-alives, comments). The probe
  only ever sends a canned, non-sensitive prompt, so assistant/user content
  in fixtures is acceptable; credentials never appear inside SSE payloads.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

__all__ = [
    "REMOVED_KEYS",
    "sanitize_raw_sse_lines",
    "sanitize_payload",
]

#: Keys removed wholesale from captured payloads.
REMOVED_KEYS = {
    "user",
    "user_info",
    "email",
    "phone",
    "avatar",
    "nickname",
    "username",
    "user_name",
    "token",
    "auth_token",
    "authorization",
    "cookie",
    "cookies",
    "cf_clearance",
    "character_id",  # account-specific character identifiers
}

_PREFIX = "data: "


class _IdMapper:
    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def placeholder(self, original: str) -> str:
        key = str(original)
        if key not in self._map:
            self._map[key] = f"SANITIZED-ID-{len(self._map) + 1}"
        return self._map[key]


def _is_id_key(key: str) -> bool:
    return key == "id" or key.endswith("_id")


def _sanitize_node(node: Any, mapper: _IdMapper) -> Any:
    if isinstance(node, dict):
        clean: dict[str, Any] = {}
        for key, value in node.items():
            if key in REMOVED_KEYS:
                continue
            if _is_id_key(key) and isinstance(value, (str, int)):
                if value is None or value == "":
                    clean[key] = value
                else:
                    clean[key] = mapper.placeholder(str(value))
            else:
                clean[key] = _sanitize_node(value, mapper)
        return clean
    if isinstance(node, list):
        return [_sanitize_node(item, mapper) for item in node]
    return node


def sanitize_payload(payload: dict[str, Any], mapper: _IdMapper | None = None) -> dict[str, Any]:
    mapper = mapper or _IdMapper()
    return _sanitize_node(payload, mapper)


def sanitize_raw_sse_lines(lines: Iterable[bytes | str]) -> list[str]:
    """Sanitize raw captured SSE lines into fixture-safe text lines."""
    mapper = _IdMapper()
    out: list[str] = []
    for line in lines:
        if isinstance(line, bytes):
            text = line.decode("utf-8", errors="replace")
        else:
            text = str(line)
        stripped = text.strip()
        if stripped.startswith(_PREFIX):
            try:
                payload = json.loads(stripped[len(_PREFIX):])
            except json.JSONDecodeError:
                # Malformed data lines are kept verbatim: parser tests need
                # them, and they cannot contain structured identifiers.
                out.append(text)
                continue
            if isinstance(payload, dict):
                sanitized = _sanitize_node(payload, mapper)
                out.append(
                    _PREFIX
                    + json.dumps(sanitized, ensure_ascii=False, separators=(", ", ": "))
                )
                continue
        out.append(text)
    return out
