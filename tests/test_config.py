"""M1 tests: the configuration boundary (app/config.py).

Security focus: the DeepSeek auth token must never leak through repr,
serialization, or error messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.backends import FakeBackend
from app.backends.deepseek_web import DeepSeekWebBackend
from app.config import (
    ConfigError,
    GatewaySettings,
    build_backend,
    load_env_file,
)

SECRET = "super-secret-token-abc123"


def env(**overrides: str) -> dict[str, str]:
    base = {"DEEPSEEK_AUTH_TOKEN": SECRET}
    base.update(overrides)
    return base


class TestFromEnv:
    def test_defaults_to_deepseek_web_with_token(self) -> None:
        settings = GatewaySettings.from_env(env())
        assert settings.backend_type == "deepseek_web"
        assert settings.deepseek_web is not None
        assert isinstance(settings.deepseek_web.auth_token, SecretStr)
        assert settings.deepseek_web.auth_token.get_secret_value() == SECRET
        assert settings.deepseek_web.cookies_file is None

    def test_missing_token_raises_config_error(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env({})
        # Message names the variable, never a secret value.
        assert "DEEPSEEK_AUTH_TOKEN" in str(excinfo.value)
        assert SECRET not in str(excinfo.value)

    def test_whitespace_only_token_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            GatewaySettings.from_env({"DEEPSEEK_AUTH_TOKEN": "   "})

    def test_cookies_file_env_parsed_to_path(self) -> None:
        settings = GatewaySettings.from_env(env(DSQG_COOKIES_FILE="C:/tmp/c.json"))
        assert settings.deepseek_web is not None
        assert settings.deepseek_web.cookies_file is not None
        assert settings.deepseek_web.cookies_file.name == "c.json"

    def test_fake_backend_selection_needs_no_token(self) -> None:
        settings = GatewaySettings.from_env({"GATEWAY_BACKEND": "fake"})
        assert settings.backend_type == "fake"
        assert settings.deepseek_web is None

    def test_unknown_backend_raises_config_error(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env({"GATEWAY_BACKEND": "openai"})
        assert "openai" in str(excinfo.value)

    def test_empty_backend_var_falls_back_to_default(self) -> None:
        settings = GatewaySettings.from_env(env(GATEWAY_BACKEND=""))
        assert settings.backend_type == "deepseek_web"

    def test_reads_os_environ_when_no_env_given(self, monkeypatch) -> None:
        monkeypatch.setenv("DEEPSEEK_AUTH_TOKEN", SECRET)
        monkeypatch.delenv("GATEWAY_BACKEND", raising=False)
        settings = GatewaySettings.from_env()
        assert settings.backend_type == "deepseek_web"


class TestSecretHygiene:
    def test_token_not_in_repr(self) -> None:
        settings = GatewaySettings.from_env(env())
        assert SECRET not in repr(settings)

    def test_token_not_in_json_dump(self) -> None:
        settings = GatewaySettings.from_env(env())
        assert SECRET not in settings.model_dump_json()
        assert SECRET not in str(settings.model_dump())

    def test_config_error_never_contains_token(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            GatewaySettings.from_env(env(GATEWAY_BACKEND="bogus"))
        assert SECRET not in str(excinfo.value)


class TestBuildBackend:
    def test_builds_fake_backend(self) -> None:
        settings = GatewaySettings.from_env({"GATEWAY_BACKEND": "fake"})
        backend = build_backend(settings)
        assert isinstance(backend, FakeBackend)
        assert backend.health_check().ready is True

    def test_builds_deepseek_web_backend(self) -> None:
        settings = GatewaySettings.from_env(env())
        backend = build_backend(settings)
        # Construction is offline; no network happens here.
        assert isinstance(backend, DeepSeekWebBackend)
        health = backend.health_check()
        assert health.backend_type == "deepseek_web"
        assert health.ready is True

    def test_deepseek_web_without_section_raises(self) -> None:
        settings = GatewaySettings(backend_type="deepseek_web")
        with pytest.raises(ConfigError):
            build_backend(settings)

    def test_unknown_backend_type_raises(self) -> None:
        settings = GatewaySettings(backend_type="mystery")
        with pytest.raises(ConfigError):
            build_backend(settings)


class TestLoadEnvFile:
    """ADR-022: repository-root .env merged under the real environment."""

    def test_parses_key_value_lines(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            "# comment\n"
            "\n"
            "GATEWAY_BACKEND=fake\n"
            "export DEEPSEEK_GATEWAY_API_KEY=abc123\n"
            "GATEWAY_MODEL_ID='quoted-alias'\n"
            'DSQG_COOKIES_FILE="C:/tmp/c.json"\n'
            "not a keyvalue line\n",
            encoding="utf-8",
        )
        merged = load_env_file(dotenv, env={})
        assert merged["GATEWAY_BACKEND"] == "fake"
        assert merged["DEEPSEEK_GATEWAY_API_KEY"] == "abc123"
        assert merged["GATEWAY_MODEL_ID"] == "quoted-alias"
        assert merged["DSQG_COOKIES_FILE"] == "C:/tmp/c.json"
        assert "not a keyvalue line" not in merged

    def test_real_environment_wins(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("GATEWAY_PORT=9999\nGATEWAY_BACKEND=fake\n", encoding="utf-8")
        merged = load_env_file(dotenv, env={"GATEWAY_PORT": "8000"})
        assert merged["GATEWAY_PORT"] == "8000"  # explicitly set → wins
        assert merged["GATEWAY_BACKEND"] == "fake"  # absent → filled in

    def test_missing_file_yields_env_unchanged(self, tmp_path: Path) -> None:
        merged = load_env_file(tmp_path / "does-not-exist.env", env={"A": "1"})
        assert merged == {"A": "1"}

    def test_from_env_consumes_the_merged_mapping(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            f"GATEWAY_BACKEND=fake\nDEEPSEEK_GATEWAY_API_KEY={SECRET}\n",
            encoding="utf-8",
        )
        settings = GatewaySettings.from_env(load_env_file(dotenv, env={}))
        assert settings.backend_type == "fake"
        assert settings.gateway_api_key is not None
        assert settings.gateway_api_key.get_secret_value() == SECRET
