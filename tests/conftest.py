"""Shared pytest fixtures for offline gateway tests."""

from __future__ import annotations

from pathlib import Path

import pytest

# Bootstrap the vendored `dsk` namespace BEFORE any test module imports it
# directly. Must stay at module top level: conftest loads before test files.
from app.backends.deepseek_web import _vendor  # noqa: E402,F401

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "deepseek_web"


@pytest.fixture()
def synthetic_dir() -> Path:
    return FIXTURE_ROOT / "synthetic"


@pytest.fixture()
def read_fixture(synthetic_dir: Path):
    def _read(name: str) -> list[str]:
        text = (synthetic_dir / name).read_text(encoding="utf-8")
        # Preserve raw lines exactly; drop only the file-level trailing newline.
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        return lines

    return _read
