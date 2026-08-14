"""M2 tests: BackendFailure → OpenAI-style HTTP error mapping."""

from __future__ import annotations

import pytest

from app.backends.errors import BackendErrorCategory, BackendFailure
from app.error_mapping import (
    ERROR_TYPE_BY_CATEGORY,
    HTTP_STATUS_BY_CATEGORY,
    backend_failure_to_response,
    openai_error_body,
)

EXPECTED_STATUS = {
    BackendErrorCategory.AUTH_INVALID: 502,
    BackendErrorCategory.RATE_LIMITED: 429,
    BackendErrorCategory.CLOUDFLARE_BLOCKED: 503,
    BackendErrorCategory.UPSTREAM_NETWORK: 502,
    BackendErrorCategory.UPSTREAM_5XX: 502,
    BackendErrorCategory.UPSTREAM_PROTOCOL: 502,
    BackendErrorCategory.CLIENT_BAD_REQUEST: 400,
    BackendErrorCategory.INTERNAL: 500,
}


class TestTables:
    def test_every_category_has_an_http_status(self) -> None:
        assert set(HTTP_STATUS_BY_CATEGORY) == set(BackendErrorCategory)

    def test_every_category_has_an_error_type(self) -> None:
        assert set(ERROR_TYPE_BY_CATEGORY) == set(BackendErrorCategory)

    @pytest.mark.parametrize("category", list(BackendErrorCategory))
    def test_status_table_matches_contract(self, category: BackendErrorCategory) -> None:
        assert HTTP_STATUS_BY_CATEGORY[category] == EXPECTED_STATUS[category]


class TestOpenAiErrorBody:
    def test_shape(self) -> None:
        body = openai_error_body("boom", "internal_error", "INTERNAL")
        assert body == {
            "error": {"message": "boom", "type": "internal_error", "code": "INTERNAL"}
        }


class TestBackendFailureToResponse:
    @pytest.mark.parametrize("category", list(BackendErrorCategory))
    def test_status_and_code_for_every_category(
        self, category: BackendErrorCategory
    ) -> None:
        failure = BackendFailure(category=category, message="detail")
        status, body = backend_failure_to_response(failure)
        assert status == EXPECTED_STATUS[category]
        error = body["error"]
        assert error["code"] == category.value
        assert error["type"] == ERROR_TYPE_BY_CATEGORY[category]
        assert error["message"] == "detail"

    def test_explicit_status_code_on_failure_does_not_override_mapping(self) -> None:
        # The upstream HTTP status rides along on the failure for metrics;
        # the client-facing status is always the category mapping.
        failure = BackendFailure(
            category=BackendErrorCategory.UPSTREAM_5XX,
            message="bad gateway upstream",
            status_code=503,
        )
        status, body = backend_failure_to_response(failure)
        assert status == 502
        assert body["error"]["code"] == "UPSTREAM_5XX"
