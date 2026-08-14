"""Opt-in diagnostic request capture (M5).

The M5 milestone connects a real Qwen Code install to the gateway and
fixtures the exact wire format it sends. This module is that capture layer:
when ``GATEWAY_DIAGNOSTICS_DIR`` is configured, the gateway appends one
sanitized JSON record per captured request to ``<dir>/requests.jsonl``
(JSON Lines, one request per line, append-only).

Privacy rules (master prompt non-negotiables):

* capture is strictly OPT-IN — nothing is written unless the directory is
  configured;
* the ``Authorization`` header VALUE is never written — only whether the
  header was present; no other header values are persisted either;
* the request BODY is written as parsed — that is the purpose of the layer
  (fixture the real agent request/history format). Bodies contain the
  client's own prompts, so point the directory at a private location.

Best-effort by construction: capture failures are logged and swallowed —
diagnostics must never break request handling.

Records capture the PARSED request (pydantic ``model_dump`` with
``exclude_none=True``), not the raw bytes: fields the client sent as null
stay omitted, but fields the client OMITTED may still appear with their
schema defaults (e.g. ``stream: false``). Qwen Code always sends ``stream``
explicitly (docs/UPSTREAM_NOTES.md), so this does not reduce real-capture
fidelity.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Mapping

__all__ = ["RequestRecorder", "REQUESTS_FILE_NAME"]

REQUESTS_FILE_NAME = "requests.jsonl"

# Header keys whose VALUES may be persisted. Everything else is reduced to
# presence/absence (Authorization) or dropped entirely.
_SAFE_HEADER_KEYS = ("content-type", "user-agent")

logger = logging.getLogger("app.diagnostics")


class RequestRecorder:
    """Appends sanitized request records to ``<directory>/requests.jsonl``.

    Thread-safe (one lock around the append). Constructing the recorder
    creates the directory eagerly, so a misconfigured path fails fast at
    startup instead of silently dropping every record.
    """

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path = self._directory / REQUESTS_FILE_NAME
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        body: Any,
    ) -> None:
        """Append one sanitized request record. Never raises."""
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "method": method,
            "path": path,
            "headers": {
                **{
                    key.replace("-", "_"): lowered[key]
                    for key in _SAFE_HEADER_KEYS
                    if key in lowered
                },
                "authorization": "present" if "authorization" in lowered else "absent",
            },
            "body": body,
        }
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "diagnostic capture failed for %s %s: %s", method, path, exc
            )
