"""Backend failure → OpenAI-style HTTP error mapping (M2).

Public error shape follows docs/API_CONTRACT.md:

.. code-block:: json

    {"error": {"message": "...", "type": "...", "code": "..."}}

HTTP status mapping follows the contract's suggested table. The ``code``
field carries the stable :class:`BackendErrorCategory` value so clients (and
later metrics) can classify without parsing messages.
"""

from __future__ import annotations

from .backends.errors import BackendErrorCategory, BackendFailure

__all__ = [
    "HTTP_STATUS_BY_CATEGORY",
    "ERROR_TYPE_BY_CATEGORY",
    "openai_error_body",
    "backend_failure_to_response",
]

HTTP_STATUS_BY_CATEGORY: dict[BackendErrorCategory, int] = {
    BackendErrorCategory.AUTH_INVALID: 502,
    BackendErrorCategory.RATE_LIMITED: 429,
    BackendErrorCategory.CLOUDFLARE_BLOCKED: 503,
    BackendErrorCategory.UPSTREAM_NETWORK: 502,
    BackendErrorCategory.UPSTREAM_5XX: 502,
    BackendErrorCategory.UPSTREAM_PROTOCOL: 502,
    BackendErrorCategory.CLIENT_BAD_REQUEST: 400,
    BackendErrorCategory.INTERNAL: 500,
}

ERROR_TYPE_BY_CATEGORY: dict[BackendErrorCategory, str] = {
    BackendErrorCategory.AUTH_INVALID: "upstream_authentication_error",
    BackendErrorCategory.RATE_LIMITED: "upstream_rate_limit_error",
    BackendErrorCategory.CLOUDFLARE_BLOCKED: "upstream_unavailable_error",
    BackendErrorCategory.UPSTREAM_NETWORK: "upstream_network_error",
    BackendErrorCategory.UPSTREAM_5XX: "upstream_server_error",
    BackendErrorCategory.UPSTREAM_PROTOCOL: "upstream_protocol_error",
    BackendErrorCategory.CLIENT_BAD_REQUEST: "invalid_request_error",
    BackendErrorCategory.INTERNAL: "internal_error",
}


def openai_error_body(message: str, type_: str, code: str) -> dict:
    """Build the OpenAI-style error envelope (never includes secrets)."""
    return {"error": {"message": message, "type": type_, "code": code}}


def backend_failure_to_response(failure: BackendFailure) -> tuple[int, dict]:
    """Map a normalized backend failure to ``(http_status, error_body)``."""
    category = failure.category
    status = HTTP_STATUS_BY_CATEGORY.get(category, 500)
    body = openai_error_body(
        message=failure.message,
        type_=ERROR_TYPE_BY_CATEGORY.get(category, "internal_error"),
        code=category.value,
    )
    return status, body
