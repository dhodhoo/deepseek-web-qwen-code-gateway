# DeepSeek Web stream fixtures

## Provenance

- `live/` — **ground truth.** Sanitized captures written by the live probe
  (`scripts/probe_deepseek.py`), verified against the real DeepSeek Web API
  on 2026-08-14. Identifiers are replaced with `SANITIZED-ID-<n>`
  placeholders (stable within a capture); personal/credential keys are
  removed (see `app/backends/deepseek_web/sanitize.py`). Current wire
  protocol: event + JSON-patch with sticky paths — documented in
  `docs/UPSTREAM_NOTES.md` ("Streaming event examples").
- `synthetic/` — hand-built payloads in the **legacy (pre-2026)**
  OpenAI-style `choices[].delta` shape. DeepSeek no longer serves this
  format; these fixtures remain as regression tests for the generic
  SSE/payload parser (`parse_sse_line`, `payload_to_events`) only.

## Fixture format

One raw SSE line per text line, as delivered by `response.iter_lines()`:

```text
data: {"p": "response/content", "o": "APPEND", "v": "OK"}
```

`event:` marker lines and blank keepalives may appear and must be tolerated.

## Tests

`tests/test_wire.py` runs every `live/stream_*.sse.txt` capture through the
protocol adapter (parametrized), so each new probe capture automatically
extends offline coverage.

## Rule

Never place a real token, cookie, account identifier, or sensitive response
into these files. Live captures must pass through the sanitization module.
