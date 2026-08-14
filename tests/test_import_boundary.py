"""M1 import-boundary guard.

AGENTS.md / QWEN.md rule: all DeepSeek private-API behavior is isolated in
``app/backends/deepseek_web``; NOTHING outside that package may import
``dsk`` (the vendored deepseek4free namespace).

This test enforces the rule statically via an AST scan of ``app/`` and
``scripts/``:

* any ``import dsk`` / ``import dsk.x`` / ``from dsk import ...`` /
  ``from dsk.x import ...`` outside ``app/backends/deepseek_web/`` fails
  the suite;
* ``tests/`` is deliberately EXEMPT: ``tests/test_errors.py`` and
  ``tests/test_backend_offline.py`` import vendored exception classes to
  prove the taxonomy mapping itself (see docs/DECISIONS.md ADR-016).

The scan is syntactic, so it also catches lazy/local imports inside
functions (the vendored client is intentionally imported lazily inside
``DeepSeekWebBackend.__init__`` and ``normalize.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = ("app", "scripts")
ALLOWED_PARTS = ("app", "backends", "deepseek_web")


def _iter_python_files():
    for root_name in SCANNED_ROOTS:
        yield from sorted((ROOT / root_name).rglob("*.py"))


def _dsk_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dsk" or alias.name.startswith("dsk."):
                    yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "dsk" or module.startswith("dsk."):
                yield node.lineno, module


def _is_inside_allowed_package(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return rel.parts[:3] == ALLOWED_PARTS


def test_no_dsk_imports_outside_deepseek_web_adapter() -> None:
    violations: list[str] = []
    for path in _iter_python_files():
        if _is_inside_allowed_package(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, module in _dsk_imports(tree):
            violations.append(
                f"{path.relative_to(ROOT)}:{lineno}: imports {module!r}"
            )
    assert not violations, (
        "Vendored 'dsk' imports leaked outside app/backends/deepseek_web "
        "(see AGENTS.md isolation rule and docs/DECISIONS.md ADR-016):\n"
        + "\n".join(violations)
    )


def test_guard_actually_detects_dsk_imports() -> None:
    # Self-test: the detector must catch both import forms, so a future
    # refactor cannot silently neuter the guard.
    sample = ast.parse(
        "import dsk\nimport dsk.api\nfrom dsk import api\nfrom dsk.api import X\n"
    )
    assert [m for _, m in _dsk_imports(sample)] == [
        "dsk",
        "dsk.api",
        "dsk",
        "dsk.api",
    ]
