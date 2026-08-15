"""Operational metrics + request instrumentation (M9, ADR-036).

One process-wide :class:`MetricsCollector`, incremented from the route
layer and the transport-retry helper, exposed read-only at
``GET /admin/metrics`` (unauthenticated like ``/health`` — local-first
gateway, and the payload carries counters/durations only, never secrets).

:class:`MetricsMiddleware` is a PURE ASGI middleware (not
``BaseHTTPMiddleware``) so SSE streaming passes through untouched: it
observes the ``http.response.start`` message for the final status —
which already reflects the app's exception handlers — and records
``METHOD path`` → status class plus wall-clock duration for every
request.

Shape of :meth:`MetricsCollector.snapshot` (pinned by tests; additive
changes only in later milestones)::

    {
      "requests": {"POST /v1/chat/completions": {"2xx": 3, "5xx": 1}},
      "request_seconds": {"count": 4, "sum": 2.5, "max": 1.3},
      "backend_attempts": 6,
      "backend_failures": {"RATE_LIMITED": 2},
      "transport_retries": 2,
      "tool_turns": 1,
      "tool_repair_retries": 1,
      "tool_repair_budget_exhausted": 0,
      "backend_attempt_seconds": {"count": 6, "sum": 9.8, "max": 4.1},
      "uptime_seconds": 42.0
    }
"""

from __future__ import annotations

import threading
import time
from typing import Any, Awaitable, Callable

__all__ = ["MetricsCollector", "MetricsMiddleware"]


def _duration_summary(count: int, total: float, maximum: float) -> dict[str, float]:
    return {
        "count": count,
        "sum": round(total, 6),
        "max": round(maximum, 6),
    }


class MetricsCollector:
    """Thread-safe in-memory counters (single lock; the gateway serializes
    backend calls anyway, so contention is negligible)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._requests: dict[str, dict[str, int]] = {}
        self._request_count = 0
        self._request_sum = 0.0
        self._request_max = 0.0
        self._backend_attempts = 0
        self._backend_failures: dict[str, int] = {}
        self._transport_retries = 0
        self._tool_turns = 0
        self._tool_repair_retries = 0
        self._tool_repair_budget_exhausted = 0
        self._attempt_count = 0
        self._attempt_sum = 0.0
        self._attempt_max = 0.0

    # ------------------------------------------------------------- requests

    def record_request(self, endpoint: str, status_code: int) -> None:
        """One HTTP request completed (status classes: ``2xx``…``5xx``)."""
        status_class = f"{status_code // 100}xx"
        with self._lock:
            bucket = self._requests.setdefault(endpoint, {})
            bucket[status_class] = bucket.get(status_class, 0) + 1

    def record_request_duration(self, seconds: float) -> None:
        with self._lock:
            self._request_count += 1
            self._request_sum += seconds
            self._request_max = max(self._request_max, seconds)

    # -------------------------------------------------------------- backend

    def record_backend_attempt(self) -> None:
        with self._lock:
            self._backend_attempts += 1

    def record_backend_failure(self, category: str) -> None:
        with self._lock:
            self._backend_failures[category] = (
                self._backend_failures.get(category, 0) + 1
            )

    def record_backend_duration(self, seconds: float) -> None:
        with self._lock:
            self._attempt_count += 1
            self._attempt_sum += seconds
            self._attempt_max = max(self._attempt_max, seconds)

    def record_transport_retry(self) -> None:
        with self._lock:
            self._transport_retries += 1

    # ---------------------------------------------------------------- tools

    def record_tool_turn(self) -> None:
        with self._lock:
            self._tool_turns += 1

    def record_tool_repair_retry(self) -> None:
        with self._lock:
            self._tool_repair_retries += 1

    def record_tool_repair_budget_exhausted(self) -> None:
        with self._lock:
            self._tool_repair_budget_exhausted += 1

    # ------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict copy (stable shape; see module docstring)."""
        with self._lock:
            return {
                "requests": {
                    endpoint: dict(bucket)
                    for endpoint, bucket in self._requests.items()
                },
                "request_seconds": _duration_summary(
                    self._request_count, self._request_sum, self._request_max
                ),
                "backend_attempts": self._backend_attempts,
                "backend_failures": dict(self._backend_failures),
                "transport_retries": self._transport_retries,
                "tool_turns": self._tool_turns,
                "tool_repair_retries": self._tool_repair_retries,
                "tool_repair_budget_exhausted": self._tool_repair_budget_exhausted,
                "backend_attempt_seconds": _duration_summary(
                    self._attempt_count, self._attempt_sum, self._attempt_max
                ),
                "uptime_seconds": round(time.time() - self._started_at, 3),
            }


class MetricsMiddleware:
    """Pure ASGI request instrumentation (streaming-safe).

    Records ``f"{method} {path}"`` → status class and the request's
    wall-clock duration. The observed status is the FINAL one: exception
    handlers run inside the wrapped app, so their mapped statuses are
    what ``http.response.start`` carries.
    """

    def __init__(
        self,
        app: Any,
        metrics: MetricsCollector,
    ) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 500)
            await send(message)

        start = time.monotonic()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.monotonic() - start
            endpoint = f"{scope.get('method', '?')} {scope.get('path', '')}"
            self.metrics.record_request(endpoint, status_holder.get("status", 500))
            self.metrics.record_request_duration(elapsed)
