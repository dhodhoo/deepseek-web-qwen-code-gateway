"""Vendor path bootstrap for the pinned deepseek4free snapshot.

The upstream project ships no packaging metadata, so it is vendored under
``vendor/deepseek4free`` (see ``vendor/deepseek4free/VENDOR_INFO.md``) and
imported as a PEP 420 namespace package by adding the vendor root to
``sys.path`` exactly once.

Importing this module is the ONLY supported way to reach ``dsk.*`` from
application code. Keeping the private-API dependency behind this seam is a
non-negotiable rule from AGENTS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Repository root (app/backends/deepseek_web/_vendor.py -> parents[3]).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENDOR_ROOT = _REPO_ROOT / "vendor" / "deepseek4free"


def ensure_vendor_path() -> None:
    """Make the vendored ``dsk`` namespace importable (idempotent)."""
    vendor_str = str(_VENDOR_ROOT)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)


ensure_vendor_path()
