"""M0 live probe for the DeepSeek Web upstream.

Verifies, with a user-provided credential:

  1. vendored client initialization
  2. chat session creation
  3. one prompt (canned by default)
  4. streamed output (events + timing)
  5. current finish behavior
  6. current upstream exceptions (normalized categories)

Captured raw SSE lines are SANITIZED (identifiers replaced with
placeholders, personal/credential keys removed) before being written as
fixtures under ``tests/fixtures/deepseek_web/live/``.

Security:
  * The auth token is read from the environment (``DEEPSEEK_AUTH_TOKEN``) or
    ``--token-file`` and is NEVER printed, logged, or persisted.
  * Session/message identifiers are never printed in full.
  * Uses a canned, non-sensitive prompt by default.

Usage:
    set DEEPSEEK_AUTH_TOKEN=<token>
    .venv\\Scripts\\python.exe scripts\\probe_deepseek.py
    # optional: --prompt "...", --thinking, --search, --output-dir DIR, --no-save

Exit codes:
    0  probe succeeded end-to-end
    2  AUTH_INVALID
    3  RATE_LIMITED
    4  CLOUDFLARE_BLOCKED
    5  other upstream failure (network / 5xx / protocol)
    6  client-side or internal error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backends.deepseek_web import DeepSeekWebBackend  # noqa: E402
from app.backends.deepseek_web.sanitize import sanitize_raw_sse_lines  # noqa: E402
from app.backends.errors import BackendErrorCategory, BackendFailure  # noqa: E402
from app.backends.events import (  # noqa: E402
    BackendMessageId,
    MessageFinished,
    ReasoningDelta,
    TextDelta,
    UnknownDelta,
)

DEFAULT_PROMPT = "Reply with exactly one word: OK"

EXIT_CODES = {
    BackendErrorCategory.AUTH_INVALID: 2,
    BackendErrorCategory.RATE_LIMITED: 3,
    BackendErrorCategory.CLOUDFLARE_BLOCKED: 4,
    BackendErrorCategory.UPSTREAM_NETWORK: 5,
    BackendErrorCategory.UPSTREAM_5XX: 5,
    BackendErrorCategory.UPSTREAM_PROTOCOL: 5,
    BackendErrorCategory.CLIENT_BAD_REQUEST: 6,
    BackendErrorCategory.INTERNAL: 6,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def read_token(args: argparse.Namespace) -> str:
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("DEEPSEEK_AUTH_TOKEN", "").strip()
    if not token:
        log(
            "ERROR: no credential. Set DEEPSEEK_AUTH_TOKEN or pass --token-file.\n"
            "See .env.example for how to obtain the DeepSeek Web auth token."
        )
        raise SystemExit(6)
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help=f"prompt to send (default: {DEFAULT_PROMPT!r})")
    parser.add_argument("--thinking", action="store_true",
                        help="enable DeepSeek thinking stream")
    parser.add_argument("--search", action="store_true",
                        help="enable DeepSeek web search")
    parser.add_argument("--token-file", default=None,
                        help="read the auth token from this file instead of the env var")
    parser.add_argument("--cookies-file", default=os.environ.get("DSQG_COOKIES_FILE") or None,
                        help="optional JSON cookies file ({\"cookies\": {...}})")
    parser.add_argument("--output-dir", default=None,
                        help="fixture output dir (default: tests/fixtures/deepseek_web/live)")
    parser.add_argument("--no-save", action="store_true",
                        help="do not write fixture files")
    args = parser.parse_args()

    token = read_token(args)
    log(f"[1/6] credential present: yes ({len(token)} chars, value hidden)")

    try:
        # -- 1. client initialization ------------------------------------
        backend = DeepSeekWebBackend(token, cookies_file=args.cookies_file)
        health = backend.health_check()
        log(f"[1/6] client init: OK ({health})")

        # -- 2. session creation -----------------------------------------
        t0 = time.perf_counter()
        session_id = backend.create_session()
        t_session = time.perf_counter() - t0
        log(f"[2/6] session created: yes (id hidden, {t_session * 1000:.0f} ms)")

        # -- 3+4. one prompt, streamed ------------------------------------
        log(f"[3/6] sending prompt: {args.prompt!r} "
            f"(thinking={args.thinking}, search={args.search})")
        raw_sink: list[bytes] = []
        counts: dict[str, int] = {}
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        unknown_kinds: list[str] = []
        finish_reason: str | None = None
        message_ids: list[str] = []
        t_start = time.perf_counter()
        first_text_at: float | None = None
        first_chunk_at: float | None = None

        for event in backend.stream_turn(
            session_id,
            args.prompt,
            thinking_enabled=args.thinking,
            search_enabled=args.search,
            raw_sink=raw_sink,
        ):
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter()
            counts[type(event).__name__] = counts.get(type(event).__name__, 0) + 1
            if isinstance(event, TextDelta):
                if first_text_at is None:
                    first_text_at = time.perf_counter()
                text_parts.append(event.text)
                print(event.text, end="", flush=True)
            elif isinstance(event, ReasoningDelta):
                thinking_parts.append(event.text)
            elif isinstance(event, UnknownDelta):
                unknown_kinds.append(event.kind)
            elif isinstance(event, MessageFinished):
                finish_reason = event.finish_reason
            elif isinstance(event, BackendMessageId):
                if event.id not in message_ids:
                    message_ids.append(event.id)
        total = time.perf_counter() - t_start
        print()  # newline after streamed text

        text_joined = "".join(text_parts)
        log(f"[4/6] stream consumed: {sum(counts.values())} events, counts={counts}")
        log(f"      text length: {len(text_joined)} chars; "
            f"thinking length: {sum(len(t) for t in thinking_parts)} chars")
        if unknown_kinds:
            log(f"      WARNING: unknown delta types observed: {sorted(set(unknown_kinds))}")
        if message_ids:
            log(f"      message ids observed in raw payloads: {len(message_ids)} (values hidden)")

        # -- 5. finish behavior --------------------------------------------
        if finish_reason is not None:
            log(f"[5/6] finish behavior: terminal MessageFinished('{finish_reason}') observed")
        else:
            log("[5/6] finish behavior: NO terminal finish event observed "
                "(stream ended without finish_reason)")
        if first_text_at is not None:
            log(f"      time-to-first-text: {(first_text_at - t_start) * 1000:.0f} ms; "
                f"total: {total * 1000:.0f} ms")
        log(f"      raw SSE lines captured: {len(raw_sink)}")

        # -- 6. fixture writing (sanitized) --------------------------------
        if args.no_save:
            log("[6/6] fixture writing: skipped (--no-save)")
        else:
            out_dir = Path(args.output_dir) if args.output_dir else (
                ROOT / "tests" / "fixtures" / "deepseek_web" / "live"
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            sanitized_lines = sanitize_raw_sse_lines(raw_sink)
            sse_path = out_dir / f"stream_{stamp}.sse.txt"
            sse_path.write_text("\n".join(sanitized_lines) + "\n", encoding="utf-8")
            meta = {
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "sanitized": True,
                "prompt": args.prompt,
                "thinking_enabled": args.thinking,
                "search_enabled": args.search,
                "event_counts": counts,
                "finish_reason": finish_reason,
                "raw_line_count": len(raw_sink),
                "message_id_count": len(message_ids),
                "unknown_delta_types": sorted(set(unknown_kinds)),
                "time_to_first_text_ms": (
                    round((first_text_at - t_start) * 1000) if first_text_at else None
                ),
                "total_ms": round(total * 1000),
                "session_create_ms": round(t_session * 1000),
                "python": sys.version.split()[0],
            }
            meta_path = out_dir / f"meta_{stamp}.json"
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            log(f"[6/6] fixtures written (sanitized):\n      {sse_path}\n      {meta_path}")

        log("PROBE RESULT: SUCCESS")
        return 0

    except BackendFailure as failure:
        log(
            f"PROBE RESULT: FAILURE category={failure.category.value} "
            f"retryable={failure.retryable} status={failure.status_code} "
            f"message={failure.message}"
        )
        if failure.cause is not None:
            log(f"      upstream exception type: {type(failure.cause).__name__}")
        return EXIT_CODES.get(failure.category, 6)
    except KeyboardInterrupt:
        log("PROBE RESULT: interrupted by user")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
