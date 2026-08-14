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

* ``GATEWAY_BACKEND``          — ``deepseek_web`` (default) or ``fake``
* ``DEEPSEEK_AUTH_TOKEN``      — required for ``deepseek_web``
* ``DSQG_COOKIES_FILE``        — optional cookies JSON path for ``deepseek_web``
* ``DEEPSEEK_GATEWAY_API_KEY`` — client→gateway auth key for ``/v1/*``
* ``GATEWAY_ALLOW_NO_AUTH``    — ``1/true/yes/on``: allow ``/v1/*`` without a
  key when no key is configured (development opt-in; secure default is deny)
* ``GATEWAY_MODEL_ID``         — advertised model alias (default ``deepseek-web``)
* ``GATEWAY_HOST``/``GATEWAY_PORT`` — bind address for ``python -m app.main``
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

DEFAULT_MODEL_ID = "deepseek-web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _parse_bool(raw: str | None, var_name: str) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigError(
        f"{var_name} must be a boolean (1/true/yes/on or 0/false/no/off), "
        f"got a value of length {len(value)}"
    )


def _parse_port(raw: str | None) -> int:
    value = (raw or "").strip() or str(DEFAULT_PORT)
    try:
        port = int(value)
    except ValueError:
        raise ConfigError(
            f"GATEWAY_PORT must be an integer, got a value of length {len(value)}"
        ) from None
    if not 1 <= port <= 65535:
        raise ConfigError("GATEWAY_PORT must be between 1 and 65535")
    return port


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
    # --- HTTP surface (M2) ------------------------------------------------
    gateway_api_key: SecretStr | None = None
    allow_no_auth: bool = False
    model_id: str = DEFAULT_MODEL_ID
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

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

        raw_key = (source.get("DEEPSEEK_GATEWAY_API_KEY") or "").strip()
        common: dict[str, object] = {
            "gateway_api_key": SecretStr(raw_key) if raw_key else None,
            "allow_no_auth": _parse_bool(
                source.get("GATEWAY_ALLOW_NO_AUTH"), "GATEWAY_ALLOW_NO_AUTH"
            ),
            "model_id": (source.get("GATEWAY_MODEL_ID") or DEFAULT_MODEL_ID).strip()
            or DEFAULT_MODEL_ID,
            "host": (source.get("GATEWAY_HOST") or DEFAULT_HOST).strip()
            or DEFAULT_HOST,
            "port": _parse_port(source.get("GATEWAY_PORT")),
        }

        if backend_type == FAKE_BACKEND_TYPE:
            return cls(backend_type=FAKE_BACKEND_TYPE, **common)  # type: ignore[arg-type]

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
                **common,  # type: ignore[arg-type]
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
