"""Backend error taxonomy (M0 subset).

Categories follow ``docs/ARCHITECTURE.md``. Every category records whether
retry/failover is appropriate. M0 only needs the mapping for the vendored
DeepSeek client's exceptions; the full taxonomy is defined now so later
milestones don't rename categories.

The DeepSeek-specific mapping from upstream exceptions to these categories
lives inside the backend adapter (``app.backends.deepseek_web``), NOT here,
so this module stays backend-agnostic.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["BackendErrorCategory", "BackendFailure", "DEFAULT_RETRYABLE"]


class BackendErrorCategory(str, Enum):
    AUTH_INVALID = "AUTH_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    CLOUDFLARE_BLOCKED = "CLOUDFLARE_BLOCKED"
    UPSTREAM_NETWORK = "UPSTREAM_NETWORK"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    UPSTREAM_PROTOCOL = "UPSTREAM_PROTOCOL"
    CLIENT_BAD_REQUEST = "CLIENT_BAD_REQUEST"
    INTERNAL = "INTERNAL"


#: Default retryability per category. Later milestones (M9+) may refine with
#: bounded retry/cooldown policies; nothing may retry infinitely.
DEFAULT_RETRYABLE: dict[BackendErrorCategory, bool] = {
    BackendErrorCategory.AUTH_INVALID: False,
    BackendErrorCategory.RATE_LIMITED: True,
    BackendErrorCategory.CLOUDFLARE_BLOCKED: False,
    BackendErrorCategory.UPSTREAM_NETWORK: True,
    BackendErrorCategory.UPSTREAM_5XX: True,
    BackendErrorCategory.UPSTREAM_PROTOCOL: False,
    BackendErrorCategory.CLIENT_BAD_REQUEST: False,
    BackendErrorCategory.INTERNAL: False,
}


class BackendFailure(Exception):
    """Normalized backend failure raised across the backend boundary.

    Stream-turn implementations may alternatively surface failures as
    :class:`app.backends.events.BackendError` events; M0's spike raises this
    exception (see docs/DECISIONS.md ADR-011).
    """

    def __init__(
        self,
        category: BackendErrorCategory,
        message: str,
        *,
        retryable: bool | None = None,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = (
            DEFAULT_RETRYABLE[category] if retryable is None else retryable
        )
        self.status_code = status_code
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"[{self.category.value}] {self.message}"
            + (f" (status={self.status_code})" if self.status_code else "")
        )
