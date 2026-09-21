# v1 release verification record

Recorded: 2026-09-21. Scope: proposal **sections 1–48** only. Section **49** is excluded from completion claims.

## Clean repository

- Product tree: committed sources under `src/`, `tests/`, `docs/`, `README.md`, `LICENSE`, `pyproject.toml`.
- Not committed: `local/proposals/**`, `local/tdp-workspace/**` (TDP harness; out of product history).

## Offline test suite

Command:

```bash
python -m pytest -q
```

Result (representative run on Python 3.14):

```text
285 passed, 1 skipped in ~65s
```

The single skip is the opt-in live Cursor smoke test when `AUTO_LOOP_LIVE_CURSOR` is unset (see below).

Session identity on provider retry is covered by `tests/unit/test_session_identity_retry.py` (scripted provider) and `tests/unit/test_subprocess_session_retry.py` (real `run_subprocess_streaming` + controller `--resume`). Supervision failure metadata (`NONZERO_EXIT`, timeouts, truncated/malformed streams) is covered by `tests/unit/test_provider_invoke_semantics.py`. Migrated v1 execution plan reviews are covered by `tests/integration/test_v1_plan_review_migration.py` and `tests/unit/test_lifecycle.py::test_load_lifecycle_enriches_migrated_plan_target_fingerprint`.

## Packaging / install smoke

Command:

```bash
python -m pytest tests/unit/test_packaging.py -q
```

Result: **2 passed** (wheel contains templates + LICENSE; clean venv `auto-loop --help`, `init`, site-packages import, uninstall).

## Wheel build

Command:

```bash
python -m pip wheel . --no-deps -w /tmp/wheels
```

Result: succeeds; templates present in wheel and packaged harness absent (`test_packaging.py`, `test_development_harness.py`).

## Static analysis

`ruff` is configured under `[tool.ruff]` in `pyproject.toml` but is **not** part of the `[project.optional-dependencies].dev` extra (dev installs only pytest and pytest-cov). Ruff was **not** executed in this environment. Optional: `pip install ruff && ruff check src tests`.

## Live Cursor smoke (§45)

| Item | Status |
|------|--------|
| Harness + runbook | Present: `src/auto_loop/live_smoke.py`, `tests/integration/test_live_cursor_smoke.py`, `docs/live-cursor-smoke.md` |
| CLI uses subprocess provider | `SubprocessCursorProvider` in `cli.py` `run` |
| Default CI / offline pytest | **Skipped** with reason: `Set AUTO_LOOP_LIVE_CURSOR=1 to opt into the real Cursor smoke test.` |
| Executed in this environment | **Yes** — 2026-09-21: `AUTO_LOOP_LIVE_CURSOR=1 python -m pytest tests/integration/test_live_cursor_smoke.py -m live_cursor -q` → **1 passed in 304.55s** (product commit `c26bbf3`, includes `--trust` for headless temp workspaces). |

To execute when prerequisites exist:

```bash
export AUTO_LOOP_LIVE_CURSOR=1
python -m pytest tests/integration/test_live_cursor_smoke.py -m live_cursor -v --tb=short
```

Do **not** treat the skipped default pytest run as a passing live check.

## Scenario coverage sign-off

All scenarios **A–T** and enhancement scenarios **U–AK** have named tests listed in [v1-traceability.md](v1-traceability.md). No scenario is intentionally weakened relative to §44 / §32.

## Documentation sign-off

| Deliverable | Path |
|-------------|------|
| Operator guide | `README.md` |
| Live smoke runbook | `docs/live-cursor-smoke.md` |
| Traceability matrix | `docs/v1-traceability.md` |
| This verification record | `docs/v1-verification.md` |

## Release readiness statement

Offline implementation, deterministic integration scenarios A–T and U–AK, packaging smoke tests, and operator documentation are **verified** as above. **Live Cursor end-to-end success** was **recorded as pass** in this environment on 2026-09-21 (opt-in pytest marker; see table above). Re-run the runbook command after material CLI or auth changes.
