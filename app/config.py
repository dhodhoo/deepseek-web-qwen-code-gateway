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
* ``GATEWAY_DIAGNOSTICS_DIR``  — optional directory for the opt-in M5
  diagnostic request capture (sanitized JSONL; see app/diagnostics.py)
* ``GATEWAY_MAX_RETRIES``      — M9 (ADR-036): max transport retries per
  request (default ``2``; ``0`` disables; bounded, never infinite)
* ``GATEWAY_RETRY_BACKOFF_SECONDS`` — M9: linear retry backoff base
  (default ``0.5``; retry *n* sleeps ``base * n``)
* ``DSQG_UPSTREAM_TIMEOUT_SECONDS`` — M9: upstream request timeout
  (default ``60``; a stall/inactivity timeout on the streaming call per
  curl_cffi semantics, total timeout on control-plane calls)
* ``DSQG_ACCOUNT_TOKENS``      — M10 (ADR-037): comma-separated DeepSeek
  auth tokens, ONE per account (multi-account routing). Mutually
  exclusive with ``DEEPSEEK_AUTH_TOKEN``; cannot be combined with
  ``DSQG_COOKIES_FILE`` (cookies are per-account credentials)
* ``DSQG_ACCOUNT_COOLDOWN_SECONDS`` — M10: 429 cooldown window per
  account (default ``300``; must be > 0)

``python -m app.main`` additionally merges the repository-root ``.env``
file under the real environment via :func:`load_env_file` (ADR-022):
variables already set in the environment always win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from .accounts import DEFAULT_ACCOUNT_COOLDOWN_SECONDS
from .backends.base import LLMBackend
from .reliability import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BACKOFF_SECONDS,
)

__all__ = [
    "ConfigError",
    "DeepSeekWebSettings",
    "GatewaySettings",
    "build_backend",
    "build_router",
    "load_env_file",
    "DEFAULT_BACKEND_TYPE",
    "FAKE_BACKEND_TYPE",
]

DEFAULT_BACKEND_TYPE = "deepseek_web"
FAKE_BACKEND_TYPE = "fake"

DEFAULT_MODEL_ID = "deepseek-web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# M9 (ADR-036): the retry defaults are imported from app/reliability.py
# (single source); the upstream timeout default is owned here.
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 60.0

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


def _parse_non_negative_int(raw: str | None, var_name: str, default: int) -> int:
    """M9 (ADR-036): integer env parsing with a default (never negative)."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ConfigError(
            f"{var_name} must be an integer, got a value of length {len(value)}"
        ) from None
    if parsed < 0:
        raise ConfigError(f"{var_name} must be >= 0 (bounded by design)")
    return parsed


def _parse_non_negative_float(raw: str | None, var_name: str, default: float) -> float:
    """M9 (ADR-036): float env parsing, >= 0 (backoff may be 0 = no delay)."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(
            f"{var_name} must be a number, got a value of length {len(value)}"
        ) from None
    if parsed < 0:
        raise ConfigError(f"{var_name} must be >= 0")
    return parsed


def _parse_seconds(raw: str | None, var_name: str, default: float) -> float:
    """M9 (ADR-036): float-seconds env parsing (must be > 0)."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        raise ConfigError(
            f"{var_name} must be a number, got a value of length {len(value)}"
        ) from None
    if not parsed > 0:
        raise ConfigError(f"{var_name} must be > 0")
    return parsed


def load_env_file(
    path: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge a ``.env`` file's ``KEY=VALUE`` pairs under ``env`` (ADR-022).

    Returns a new mapping: ``env`` (default ``os.environ``) with file
    values added for keys that are NOT already set — an explicitly set
    environment variable always wins (standard dotenv semantics). ``path``
    defaults to the repository-root ``.env``, resolved from the ``app``
    package location so it works regardless of the current working
    directory. A missing/unreadable file simply yields ``env`` unchanged.

    Parsing is deliberately minimal: one ``KEY=VALUE`` per line; blank
    lines and ``#`` comments ignored; a leading ``export `` tolerated;
    one pair of surrounding single/double quotes stripped from the value;
    lines without ``=`` skipped; no variable expansion.
    """
    base: dict[str, str] = dict(os.environ if env is None else env)
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return base
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        base.setdefault(key.strip(), value)
    return base


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
    # --- M10 (ADR-037): multi-account routing ---------------------------
    #: ALL configured DeepSeek accounts (``None`` for the fake backend).
    #: Single-account configs (``DEEPSEEK_AUTH_TOKEN``) keep ``None`` here
    #: and expose the one account through ``deepseek_web`` exactly as
    #: before M10; ``DSQG_ACCOUNT_TOKENS`` fills this tuple (in config
    #: order) and ``deepseek_web`` mirrors its first entry.
    deepseek_accounts: tuple[DeepSeekWebSettings, ...] | None = None
    account_cooldown_seconds: float = DEFAULT_ACCOUNT_COOLDOWN_SECONDS
    # --- HTTP surface (M2) ------------------------------------------------
    gateway_api_key: SecretStr | None = None
    allow_no_auth: bool = False
    model_id: str = DEFAULT_MODEL_ID
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # --- M5: opt-in diagnostic request capture (app/diagnostics.py) ------
    diagnostics_dir: Path | None = None
    # --- M9 (ADR-036): reliability hardening ------------------------------
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    upstream_timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS

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
        diagnostics_raw = (source.get("GATEWAY_DIAGNOSTICS_DIR") or "").strip()
        common["diagnostics_dir"] = (
            Path(diagnostics_raw) if diagnostics_raw else None
        )
        # M9 (ADR-036): bounded retry policy + upstream timeout.
        common["max_retries"] = _parse_non_negative_int(
            source.get("GATEWAY_MAX_RETRIES"),
            "GATEWAY_MAX_RETRIES",
            DEFAULT_MAX_RETRIES,
        )
        common["retry_backoff_seconds"] = _parse_non_negative_float(
            source.get("GATEWAY_RETRY_BACKOFF_SECONDS"),
            "GATEWAY_RETRY_BACKOFF_SECONDS",
            DEFAULT_RETRY_BACKOFF_SECONDS,
        )
        common["upstream_timeout_seconds"] = _parse_seconds(
            source.get("DSQG_UPSTREAM_TIMEOUT_SECONDS"),
            "DSQG_UPSTREAM_TIMEOUT_SECONDS",
            DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
        )
        # M10 (ADR-037): 429 cooldown window for the account router.
        common["account_cooldown_seconds"] = _parse_seconds(
            source.get("DSQG_ACCOUNT_COOLDOWN_SECONDS"),
            "DSQG_ACCOUNT_COOLDOWN_SECONDS",
            DEFAULT_ACCOUNT_COOLDOWN_SECONDS,
        )

        if backend_type == FAKE_BACKEND_TYPE:
            return cls(backend_type=FAKE_BACKEND_TYPE, **common)  # type: ignore[arg-type]

        if backend_type == DEFAULT_BACKEND_TYPE:
            token = (source.get("DEEPSEEK_AUTH_TOKEN") or "").strip()
            tokens_raw = (source.get("DSQG_ACCOUNT_TOKENS") or "").strip()
            if token and tokens_raw:
                raise ConfigError(
                    "DEEPSEEK_AUTH_TOKEN and DSQG_ACCOUNT_TOKENS are "
                    "mutually exclusive; configure exactly one mechanism "
                    "(single account OR multi-account)"
                )
            cookies_raw = (source.get("DSQG_COOKIES_FILE") or "").strip()
            if tokens_raw:
                # M10 (ADR-037): multi-account config. One token per
                # account; validation messages name the variable only,
                # never a value (secrets never in errors).
                entries = [part.strip() for part in tokens_raw.split(",")]
                if any(not entry for entry in entries):
                    raise ConfigError(
                        "DSQG_ACCOUNT_TOKENS contains an empty entry "
                        "(check for stray commas)"
                    )
                if len(set(entries)) != len(entries):
                    raise ConfigError(
                        "DSQG_ACCOUNT_TOKENS contains duplicate tokens; "
                        "every account needs its own credential"
                    )
                if cookies_raw:
                    raise ConfigError(
                        "DSQG_COOKIES_FILE cannot be combined with "
                        "DSQG_ACCOUNT_TOKENS: cookies are per-account "
                        "credentials and per-account cookie files arrive "
                        "with the persistence milestone"
                    )
                accounts = tuple(
                    DeepSeekWebSettings(auth_token=SecretStr(entry))
                    for entry in entries
                )
                return cls(
                    backend_type=DEFAULT_BACKEND_TYPE,
                    deepseek_web=accounts[0],
                    deepseek_accounts=accounts,
                    **common,  # type: ignore[arg-type]
                )
            if not token:
                raise ConfigError(
                    "DEEPSEEK_AUTH_TOKEN is required for backend "
                    "'deepseek_web' (see .env.example); or set "
                    "GATEWAY_BACKEND=fake for credential-free development"
                )
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


def _build_deepseek_backend(
    settings: GatewaySettings, account_settings: DeepSeekWebSettings
) -> LLMBackend:
    """One ``DeepSeekWebBackend`` for one account's settings (M10).

    Each account gets its OWN backend instance — own vendored client,
    own PoW solver, own call gate — so accounts are concurrency-isolated
    (ADR-037 point 2). The vendored import stays lazy (fake-backend
    development never touches the private-API dependency path).
    """
    from .backends.deepseek_web import DeepSeekWebBackend

    return DeepSeekWebBackend(
        account_settings.auth_token.get_secret_value(),
        cookies_file=account_settings.cookies_file,
        # M9 (ADR-036): bounded upstream timeout (stall semantics on
        # the streaming call; see vendor/deepseek4free/dsk/api.py).
        request_timeout=settings.upstream_timeout_seconds,
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
        return _build_deepseek_backend(settings, settings.deepseek_web)

    raise ConfigError(f"Unknown backend_type {settings.backend_type!r}")


def build_router(settings: GatewaySettings):
    """Construct the account router for the configured backend (M10).

    Single source of truth for "which accounts exist": ``DEEPSEEK_AUTH_TOKEN``
    (or an injected backend) yields the one-account router with id
    ``default`` — byte-for-byte the pre-M10 behavior; ``DSQG_ACCOUNT_TOKENS``
    yields ``acct-1..N`` in config order, each with its own backend
    instance. Account state itself (healthy/cooldown/invalid) starts
    fresh on every restart — the registry is in-memory (ADR-037).
    """
    from .accounts import (
        AccountRecord,
        AccountRouter,
    )

    if settings.backend_type == FAKE_BACKEND_TYPE:
        from .backends.fake import FakeBackend

        return AccountRouter.single(
            FakeBackend(), cooldown_seconds=settings.account_cooldown_seconds
        )

    if settings.backend_type == DEFAULT_BACKEND_TYPE:
        if settings.deepseek_accounts:
            records = [
                AccountRecord(
                    id=f"acct-{index}",
                    label=f"DeepSeek account {index}",
                    backend=_build_deepseek_backend(settings, account_settings),
                )
                for index, account_settings in enumerate(
                    settings.deepseek_accounts, start=1
                )
            ]
            return AccountRouter(
                records, cooldown_seconds=settings.account_cooldown_seconds
            )
        if settings.deepseek_web is None:
            raise ConfigError(
                "backend 'deepseek_web' requires deepseek_web settings; "
                "build settings via GatewaySettings.from_env()"
            )
        return AccountRouter.single(
            _build_deepseek_backend(settings, settings.deepseek_web),
            cooldown_seconds=settings.account_cooldown_seconds,
        )

    raise ConfigError(f"Unknown backend_type {settings.backend_type!r}")
