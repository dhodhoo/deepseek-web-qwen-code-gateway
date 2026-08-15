"""Bounded transport retry for pre-byte backend interactions (M9, ADR-036).

The ONLY retry loop in the gateway besides the tool-repair budget
(app/server.py, ADR-028/035 — a SEMANTIC retry, orthogonal to this one).
Transport retry absorbs TRANSIENT upstream failures — rate-limit windows,
socket stalls, truncated streams — that the taxonomy marks retryable
(:data:`app.backends.errors.DEFAULT_RETRYABLE`), before the client ever
sees a status.

Hard guarantees (M9 exit criteria):

* **Bounded.** At most ``policy.max_retries`` retries — at most
  ``max_retries + 1`` attempts total. There is no configuration under
  which this loop runs unbounded.
* **Deterministic.** Linear backoff ``backoff_seconds * retry_number``
  (0.5 s, 1.0 s at defaults); no jitter — a local-first single-client
  gateway values exact test pins over decorrelation.
* **Taxonomy-driven.** Only failures whose ``retryable`` flag is truthy
  are retried; everything else re-raises immediately (exactly ONE
  attempt — no hot loop on AUTH_INVALID / CLOUDFLARE_BLOCKED /
  CLIENT_BAD_REQUEST / INTERNAL).
* **Status-preserving.** The final failure re-raises UNCHANGED, so
  app/error_mapping.py produces exactly the same HTTP status as the
  no-retry path — retry changes latency before failure, never the
  failure's public shape.
* **Scope.** Callers wrap ONLY pre-byte backend interactions (stream
  priming, buffered tool-turn drains, session creation). Mid-stream
  failures (HTTP 200 already committed) are never retried — replaying
  deltas into a committed stream would corrupt the wire.

``sleep`` and ``on_retry`` are injectable so tests run without real
delays and the server can attach its log line
(``transport retry %d/%d after %.2fs: category=%s``); ``metrics``
(optionally) receives per-attempt accounting
(:mod:`app.metrics`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .backends.errors import BackendFailure

__all__ = ["RetryPolicy", "with_transport_retry"]

T = TypeVar("T")

DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5


@dataclass(frozen=True)
class RetryPolicy:
    """Numeric bounds for one request's transport retries.

    ``max_retries`` = number of RETRIES after the first attempt (so
    ``max_retries + 1`` total attempts; ``0`` disables retry while
    keeping the deterministic single-attempt path). ``backoff_seconds``
    is the LINEAR base: retry *n* sleeps ``backoff_seconds * n``.
    """

    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS


def with_transport_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, float, BackendFailure], None] | None = None,
    metrics=None,
) -> T:
    """Run ``fn``; retry bounded-ly on retryable :class:`BackendFailure`.

    ``on_retry(retry_number, delay_seconds, failure)`` fires AFTER the
    decision to retry, BEFORE the sleep. ``metrics`` (an
    :class:`app.metrics.MetricsCollector`, optional) records one backend
    attempt per attempt, every backend failure by category (retried or
    final), one transport retry per retry, and the per-attempt duration.
    Non-``BackendFailure`` exceptions propagate immediately and are
    never retried.
    """
    retries = 0
    while True:
        if metrics is not None:
            metrics.record_backend_attempt()
        start = time.monotonic()
        try:
            result = fn()
        except BackendFailure as failure:
            if metrics is not None:
                metrics.record_backend_failure(failure.category.value)
                metrics.record_backend_duration(time.monotonic() - start)
            if not failure.retryable or retries >= policy.max_retries:
                raise
            retries += 1
            delay = policy.backoff_seconds * retries
            if on_retry is not None:
                on_retry(retries, delay, failure)
            if metrics is not None:
                metrics.record_transport_retry()
            sleep(delay)
        else:
            if metrics is not None:
                metrics.record_backend_duration(time.monotonic() - start)
            return result
