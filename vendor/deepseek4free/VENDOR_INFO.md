# Vendored: deepseek4free

- **Upstream:** https://github.com/xtekky/deepseek4free
- **License:** MIT (see `LICENSE`)
- **Pinned commit:** `4ae47bbb144f33b0ba855af9d1b0206ea794e16c` (2025-02-09, "Merge pull request #9 from theguy000/main — Added CloudFlare Bypass")
- **Vendored on:** 2026-08-14 (M0)
- **File checksums (SHA256) at vendoring time, post local patches:**
  - `dsk/api.py`: patched — see "Local patches" below.
  - `dsk/wasm/sha3_wasm_bg.7b9ca65ddd.wasm`: `b3fca8cc072c1defbd60c02266a8e48bd307a1804aaff4314900aea720e72f7d` (unmodified)

## Why vendored

Upstream ships no `pyproject.toml`/`setup.py`, so it cannot be pip-installed
from git. Vendoring pins the exact revision we verified during M0 and keeps all
private-API behavior isolated inside this directory, as required by
`AGENTS.md` ("Keep DeepSeek private-API behavior isolated behind
`DeepSeekWebBackend`").

`dsk` is imported as a PEP 420 namespace package by
`app/backends/deepseek_web/_vendor.py` (no `__init__.py` exists upstream).

## Local patches (search for `[DSQG-VENDOR-PATCH]`)

1. `dsk/api.py`: replaced `import pkg_resources` with
   `importlib.metadata` for the curl-cffi version check. `pkg_resources` was
   removed from setuptools >= 81 and is absent from stock Python 3.12+ venvs.
   The check only prints a warning, so runtime behavior is unchanged.

Any future change to vendored files MUST add a `[DSQG-VENDOR-PATCH]` marker
and a note here plus an ADR entry in `docs/DECISIONS.md`.

## Dependencies actually used by the gateway

- `dsk/api.py` — client (imported by `app.backends.deepseek_web`)
- `dsk/pow.py` + `dsk/wasm/*.wasm` — proof-of-work solver (wasmtime + numpy)

Not used by the M0 gateway (kept for completeness of the pinned snapshot):

- `dsk/bypass.py`, `dsk/CloudflareBypasser.py`, `dsk/run_and_get_cookies.py`
  — browser-assisted `cf_clearance` cookie capture (nodriver/DrissionPage).
- `dsk/server.py` — unrelated generic Cloudflare-bypass microservice.
