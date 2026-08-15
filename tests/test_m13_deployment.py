"""M13 deployment artifact contract tests (ADR-040)."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_is_non_root_and_runs_gateway() -> None:
    dockerfile = _text("Dockerfile")
    assert "FROM python:3.12-slim" in dockerfile
    assert "pip install --no-cache-dir ." in dockerfile
    assert "useradd --create-home --uid 10001" in dockerfile
    assert "USER gateway" in dockerfile
    assert 'CMD ["python", "-m", "app.main"]' in dockerfile
    assert "COPY .env" not in dockerfile


def test_compose_publishes_local_port_volume_and_healthcheck() -> None:
    compose = _text("compose.yaml")
    assert "env_file:" in compose
    assert "- .env" in compose
    assert 'GATEWAY_HOST: 0.0.0.0' in compose
    assert '"${GATEWAY_PUBLISH_HOST:-127.0.0.1}:${GATEWAY_PUBLISH_PORT:-8000}:8000"' in compose
    assert "gateway-data:/var/lib/deepseek-qwen-gateway" in compose
    assert "gateway-data:" in compose
    assert "http://127.0.0.1:8000/health" in compose
    assert "start_period:" in compose


def test_dockerignore_excludes_runtime_secrets_and_local_artifacts() -> None:
    dockerignore = _text(".dockerignore")
    for entry in (".env", ".env.*", "*.token", "cookies.json", ".venv", "tests"):
        assert entry in dockerignore
    assert "!.env.example" in dockerignore


def test_operator_docs_cover_exit_path_and_qwen_setup() -> None:
    docs = _text("docs/OPERATIONS.md")
    for marker in (
        "docker compose up -d --build",
        "docker compose up -d",
        "http://127.0.0.1:8000/health",
        "baseUrl: http://127.0.0.1:8000/v1",
        "GATEWAY_PUBLISH_PORT",
        "gateway-data",
        "docker compose logs",
        "Troubleshooting",
    ):
        assert marker in docs
    assert ".env" in docs
    assert "troubleshooting" in docs.lower()
    assert "never" in docs.lower() and "image" in docs.lower()


def test_env_example_documents_container_port_and_runtime_secrets() -> None:
    env = _text(".env.example")
    assert "GATEWAY_PUBLISH_HOST=127.0.0.1" in env
    assert "GATEWAY_PUBLISH_PORT=8000" in env
    assert "DEEPSEEK_AUTH_TOKEN=" in env
    assert "DEEPSEEK_GATEWAY_API_KEY=" in env
    assert "GATEWAY_HOST=127.0.0.1" in env
