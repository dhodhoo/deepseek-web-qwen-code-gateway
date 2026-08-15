# M8 acceptance fixture — buggy textstats repository

This directory is the deterministic bug-fix benchmark for milestone M8 of
the DeepSeek Qwen Gateway (see `../../docs/ROADMAP.md`). It is committed
with exactly ONE bug in the library code; the test suite fails until the
bug is fixed.

## Intended use

Start Qwen Code from THIS directory (so this fixture, not the parent
project, is the working tree) and give it the milestone prompt:

```text
Find and fix the bug, then run the tests and explain what changed.
```

## Determinism

- Python standard library only — no installs, no network, no clock.
- Pure functions with fixed inputs: results never vary between runs.
- Exactly one failing test, one root cause, one-line fix.

## Resetting after a run

An acceptance run FIXES the library and leaves this directory modified.
Restore the original buggy state for the next run from the repository
root:

```text
git checkout -- acceptance/m8-buggy-repo
```

## What counts as a pass

See the "M8 acceptance" checklist in
`../../docs/QWEN_CODE_INTEGRATION.md`.
