# Live Cursor smoke test (proposal §45)

This is an **optional**, **environment-gated** end-to-end check that runs real Cursor worker and reviewer sessions against a disposable Git repository. Normal CI and `pytest -q` **do not** run it.

## Prerequisites

- Python 3.12+ with `auto-loop` installed (`pip install -e ".[dev]"` or a wheel).
- Cursor **agent CLI** on `PATH` (`agent` or `cursor-agent`).
- Valid Cursor authentication for the CLI (same as interactive `agent -p` use).
- Network access and model quota for multiple long-running turns.
- **Isolated directory** — the test creates a new repo under pytest `tmp_path`; do not point it at production worktrees.

## Opt-in

```bash
export AUTO_LOOP_LIVE_CURSOR=1
```

Without this variable, the smoke test is **skipped** with an explicit reason (not treated as a pass).

## Command

From the repository root:

```bash
export AUTO_LOOP_LIVE_CURSOR=1
python -m pytest tests/integration/test_live_cursor_smoke.py -m live_cursor -v --tb=short
```

Equivalent after install:

```bash
pytest tests/integration/test_live_cursor_smoke.py -m live_cursor -v
```

## What it does

1. Creates a temporary Git repo with a tiny `src/` layout.
2. Runs `auto-loop init` and writes a deterministic **greet** task/plan.
3. Runs `auto-loop run` via `SubprocessCursorProvider` (real CLI, not the fake provider).
4. On `COMPLETE`, asserts (from durable artifacts):
   - distinct worker and reviewer session IDs;
   - plan approval recorded;
   - multiple review markdown files;
   - `completion.json` `final_commit` equals current `HEAD`;
   - `auto-loop doctor` passes on the repo.

## Cost and time

Expect **many** agent turns (plan, implementation batch, possible revisions, final review). Budget **tens of minutes** and non-trivial API usage. Use `--max-turns` / config limits only by changing the test options if you extend the harness.

## Evidence and diagnosis

- Per-turn logs: `.auto-loop/runtime/runs/<lifecycle_id>/` inside the temp repo (preserved until pytest tmp cleanup).
- Events: `.auto-loop/runtime/events.jsonl`
- Reviews: `.auto-loop/reviews/*.md`
- On failure, pytest writes `smoke-evidence.json` next to the temp repo parent with session ids and notes.

## Cleanup

Pytest removes `tmp_path` after the test. No manual cleanup unless you copy the temp directory for debugging (`pytest --basetemp=...`).

## Blocked environments

If Cursor is missing, unauthenticated, quota-blocked, or unsupported, the gate skips or the run exits non-zero. **Do not** report skipped or failed live runs as passing offline verification. Record the concrete blocker (CLI message, exit code, doctor output) in release evidence.

## Offline suite

```bash
python -m pytest -q
```

runs **194+** tests without live Cursor. Packaging smoke: `python -m pytest tests/unit/test_packaging.py -q`.
