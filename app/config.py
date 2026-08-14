"""Configuration boundary (M1).

Single place where environment settings become gateway objects:

* :class:`GatewaySettings` — validated settings (pydantic v2); the DeepSeek
  auth token is held as :class:`pydantic.SecretStr`, so it is masked in
  ``repr`` and JSON dumps by construction;
* :meth:`GatewaySettings.from_env` — environment parsing with an injectable
  ``env`` mapping (tests never need to monkeypatch);
* :func:`build_backend` — settings → :class:`app.backends.base.LLMBackend`
  factory (the backend selection registry).

Security rules honored here:

* secrets are never logged and never appear in error messages;
* the vendored DeepSeek stack is imported lazily (only when the
  ``deepseek_web`` backend is actually built), so ``GATEWAY_BACKEND=fake``
  development works without the private-API dependency path.

Environment variables (see .env.example):

* ``GATEWAY_BACKEND``       — ``deepseek_web`` (default) or ``fake``
* ``DEEPSEEK_AUTH_TOKEN``   — required for ``deepseek_web``
* ``DSQG_COOKIES_FILE``     — optional cookies JSON path for ``deepseek_web``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from .backends.base import LLMBackend

__all__ = [
    "ConfigError",
    "DeepSeekWebSettings",
    "GatewaySettings",
    "build_backend",
    "DEFAULT_BACKEND_TYPE",
    "FAKE_BACKEND_TYPE",
]

DEFAULT_BACKEND_TYPE = "deepseek_web"
FAKE_BACKEND_TYPE = "fake"


class ConfigError(ValueError):
    """Invalid or missing configuration. Messages never contain secrets."""


class DeepSeekWebSettings(BaseModel):
    """Settings for the DeepSeek Web backend. Secret-aware by construction."""

    model_config = ConfigDict(frozen=True)

    auth_token: SecretStr
    cookies_file: Path | None = None


class GatewaySettings(BaseModel):
    """Root gateway settings, parsed from the environment."""

    model_config = ConfigDict(frozen=True)

    backend_type: str = DEFAULT_BACKEND_TYPE
    deepseek_web: DeepSeekWebSettings | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GatewaySettings":
        """Parse settings from ``env`` (defaults to ``os.environ``).

        Raises :class:`ConfigError` (a ``ValueError``) on missing/invalid
        configuration. Error messages reference variable names only, never
        values.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        backend_type = (
            source.get("GATEWAY_BACKEND") or DEFAULT_BACKEND_TYPE
        ).strip() or DEFAULT_BACKEND_TYPE

        if backend_type == FAKE_BACKEND_TYPE:
            return cls(backend_type=FAKE_BACKEND_TYPE)

        if backend_type == DEFAULT_BACKEND_TYPE:
            token = (source.get("DEEPSEEK_AUTH_TOKEN") or "").strip()
            if not token:
                raise ConfigError(
                    "DEEPSEEK_AUTH_TOKEN is required for backend "
                    "'deepseek_web' (see .env.example); or set "
                    "GATEWAY_BACKEND=fake for credential-free development"
                )
            cookies_raw = (source.get("DSQG_COOKIES_FILE") or "").strip()
            return cls(
                backend_type=DEFAULT_BACKEND_TYPE,
                deepseek_web=DeepSeekWebSettings(
                    auth_token=SecretStr(token),
                    cookies_file=Path(cookies_raw) if cookies_raw else None,
                ),
            )

        raise ConfigError(
            f"Unknown GATEWAY_BACKEND {backend_type!r}; expected "
            f"{DEFAULT_BACKEND_TYPE!r} or {FAKE_BACKEND_TYPE!r}"
        )


def build_backend(settings: GatewaySettings) -> LLMBackend:
    """Construct the configured backend (the backend selection registry).

    Raises :class:`ConfigError` for unknown/incomplete settings. Backend
    construction failures with the normalized taxonomy raise
    :class:`app.backends.errors.BackendFailure` from the backend itself.
    """
    if settings.backend_type == FAKE_BACKEND_TYPE:
        from .backends.fake import FakeBackend

        return FakeBackend()

    if settings.backend_type == DEFAULT_BACKEND_TYPE:
        if settings.deepseek_web is None:
            raise ConfigError(
                "backend 'deepseek_web' requires deepseek_web settings; "
                "build settings via GatewaySettings.from_env()"
            )
        from .backends.deepseek_web import DeepSeekWebBackend

        return DeepSeekWebBackend(
            settings.deepseek_web.auth_token.get_secret_value(),
            cookies_file=settings.deepseek_web.cookies_file,
        )

    raise ConfigError(f"Unknown backend_type {settings.backend_type!r}")
