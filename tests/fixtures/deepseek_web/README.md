# DeepSeek Web stream fixtures

## Provenance

- `synthetic/` — hand-built SSE payloads matching the wire contract observed
  in the vendored upstream parser (`dsk/api.py::_parse_chunk`) at commit
  `4ae47bbb` and the DeepSeek Chat Completions-style framing it expects.
  **Synthetic fixtures are provisional:** they exist so parser tests run
  offline before live capture. The M0 live probe (`scripts/probe_deepseek.py`)
  validates or corrects them.
- `live/` — sanitized captures written by the live probe. Identifiers are
  replaced with `SANITIZED-ID-<n>` placeholders; personal/credential keys are
  removed (see `app/backends/deepseek_web/sanitize.py`).

## Fixture format

One raw SSE line per text line, as delivered by `response.iter_lines()`
(no trailing newlines inside a line):

```text
data: {"id": "...", "choices": [{"delta": {"content": "...", "type": "text"}, "finish_reason": null}]}
```

Only `data: ` lines carry JSON payloads. Blank lines, `event:`/`id:` lines,
and comments may appear in real streams and must be tolerated.

## Rule

Never place a real token, cookie, account identifier, or sensitive response
into these files. Live captures must pass through the sanitization module.
