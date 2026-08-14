"""Offline tests for upstream exception normalization into the taxonomy."""

from __future__ import annotations

import pytest
from dsk.api import (  # vendored
    APIError,
    AuthenticationError,
    CloudflareError,
    NetworkError,
    RateLimitError,
)

from app.backends.deepseek_web.normalize import (
    RawStreamParseError,
    classify_upstream_exception,
)
from app.backends.errors import BackendErrorCategory


class TestClassifyUpstreamException:
    def test_authentication_error(self) -> None:
        failure = classify_upstream_exception(
            AuthenticationError("Invalid or expired authentication token")
        )
        assert failure.category is BackendErrorCategory.AUTH_INVALID
        assert failure.retryable is False

    def test_rate_limit(self) -> None:
        failure = classify_upstream_exception(RateLimitError("API rate limit exceeded"))
        assert failure.category is BackendErrorCategory.RATE_LIMITED
        assert failure.retryable is True
        assert failure.status_code == 429

    def test_cloudflare_error(self) -> None:
        failure = classify_upstream_exception(CloudflareError("blocked"))
        assert failure.category is BackendErrorCategory.CLOUDFLARE_BLOCKED
        assert failure.retryable is False

    def test_network_error(self) -> None:
        failure = classify_upstream_exception(NetworkError("connection reset"))
        assert failure.category is BackendErrorCategory.UPSTREAM_NETWORK
        assert failure.retryable is True

    def test_api_error_5xx(self) -> None:
        failure = classify_upstream_exception(APIError("Server error", status_code=502))
        assert failure.category is BackendErrorCategory.UPSTREAM_5XX
        assert failure.retryable is True
        assert failure.status_code == 502

    def test_api_error_other_status(self) -> None:
        failure = classify_upstream_exception(APIError("forbidden", status_code=403))
        assert failure.category is BackendErrorCategory.UPSTREAM_PROTOCOL
        assert failure.status_code == 403
        assert failure.retryable is False

    def test_api_error_parse_failure(self) -> None:
        failure = classify_upstream_exception(
            APIError("Invalid JSON response from server")
        )
        assert failure.category is BackendErrorCategory.UPSTREAM_PROTOCOL

    def test_api_error_cloudflare_giveup(self) -> None:
        failure = classify_upstream_exception(
            APIError("Failed to bypass Cloudflare protection after multiple attempts")
        )
        assert failure.category is BackendErrorCategory.CLOUDFLARE_BLOCKED

    def test_raw_stream_parse_error(self) -> None:
        failure = classify_upstream_exception(RawStreamParseError("bad json"))
        assert failure.category is BackendErrorCategory.UPSTREAM_PROTOCOL

    def test_value_error_is_client_bad_request(self) -> None:
        failure = classify_upstream_exception(ValueError("Prompt must be a non-empty string"))
        assert failure.category is BackendErrorCategory.CLIENT_BAD_REQUEST

    def test_unknown_exception_is_internal(self) -> None:
        failure = classify_upstream_exception(RuntimeError("boom"))
        assert failure.category is BackendErrorCategory.INTERNAL
        assert failure.retryable is False

    def test_failure_message_contains_no_token(self) -> None:
        # Vendored messages never echo tokens; guard that our wrapper keeps it that way.
        failure = classify_upstream_exception(
            AuthenticationError("Invalid or expired authentication token")
        )
        assert "eyJ" not in str(failure)  # JWT-ish substrings never present

    def test_str_includes_category(self) -> None:
        failure = classify_upstream_exception(RateLimitError("x"))
        assert "RATE_LIMITED" in str(failure)


@pytest.mark.parametrize(
    "category",
    list(BackendErrorCategory),
)
def test_every_category_has_default_retryability(category) -> None:
    from app.backends.errors import DEFAULT_RETRYABLE

    assert category in DEFAULT_RETRYABLE
