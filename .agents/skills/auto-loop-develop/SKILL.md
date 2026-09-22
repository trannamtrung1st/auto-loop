---
name: auto-loop-develop
description: >-
  Work on the auto-loop repository: discover architecture, implement controller
  changes, migrations, tests, and docs while keeping agent semantics out of the
  scheduler. Use when building or fixing auto-loop itself.
---

# auto-loop develop

You are contributing to the **auto-loop** Python package in this repository — not running a target task lifecycle. Root `AGENTS.md` and this skill apply only here. Runtime agents on a user repo use the configured artifact root and the explicit run YAML. The auto-loop controller does not package, install, inject, or explicitly load this contributor skill (Cursor may still discover repo guidance when this repository is the workspace).

## Before you change code

1. Read `AGENTS.md`, `README.md`, and relevant `docs/` / `local/proposals/` material.
2. Map **invariants** (session identity, Git approved baseline, reviewer-only completion, four session slots, planning retirement).
3. Identify modules: `lifecycle.py`, `loop.py`, `models.py`, `protocol.py`, `git.py`, `review_targets.py`, providers, templates under `src/auto_loop/templates/`.
4. Find tests: unit under `tests/unit/`, scenarios under `tests/integration/`.

## Controller vs agents

- **Controller (your code):** phase/slot dispatch, protocol parsing, Git range normalization, review-target fingerprints, protection, persistence, retries, limits, exit codes.
- **Agents (runtime prompts):** planning, implementation, review reasoning, batch boundaries, when to request review.

Do not add semantic orchestration (task DAGs, plan-item scheduling, “major plan change” classifiers).

## Implementation checklist

| Area | Touch points |
|------|----------------|
| Lifecycle / slots | `lifecycle.py`, `loop.py`, `status_report.py`, `doctor.py` |
| Protocol / results | `models.py`, `protocol.py`, `templates/protocol/*` |
| Git / reviews | `git.py`, `review_targets.py`, `reviews.py`, `protection.py` |
| Cursor provider | `providers/cursor.py`, `subprocess_cursor.py`, `supervision.py` |
| Config / init | `config.py`, `init_cmd.py`, templates |
| Migrations | `lifecycle.migrate_lifecycle_data`, tests in `test_lifecycle.py` |

Add or extend **fake-provider integration tests** for behavior changes. Run only the tests focused on the affected behavior by default. Do not run the full offline suite (`python -m pytest -q`) unless the user explicitly requests it.

## Development harness rules

- Contributor skills live only at repository root: `AGENTS.md`, `.agents/skills/**`.
- Do not package, install, or copy them into `src/auto_loop`, wheels, or `auto-loop init` output.
- Target repos may have their own `AGENTS.md`; that is unrelated to this harness.

## Verification

Install development dependencies only when the environment needs them:

```bash
python -m pip install -e ".[dev]"
```

Choose focused checks that cover the changed behavior, for example:

```bash
python -m pytest tests/unit/test_<affected_area>.py -q
python -m pytest tests/unit/test_packaging.py tests/unit/test_development_harness.py -q
ruff check <changed-python-paths>
```

The packaging/harness tests above are relevant when those areas change; they are not a default requirement for unrelated work. Run `python -m pytest -q` only on explicit user request. Live Cursor smoke is also opt-in (`AUTO_LOOP_LIVE_CURSOR=1`); do not claim it passed unless you ran it. GitHub Actions runs the offline suite, packaging tests, and Ruff.
