---
name: auto-loop-develop
description: >-
  Work on the auto-loop repository: discover architecture, implement controller
  changes, migrations, tests, and docs while keeping agent semantics out of the
  scheduler. Use when building or fixing auto-loop itself.
---

# auto-loop develop

You are contributing to the **auto-loop** Python package in this repository — not running a target task lifecycle. Root `AGENTS.md` and this skill apply only here. Runtime agents on a user repo use `.auto-loop/` templates and `context.yaml`; they never load this skill.

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

Add or extend **fake-provider integration tests** for behavior changes. Run focused tests, then `python -m pytest -q`.

## Development harness rules

- Contributor skills live only at repository root: `AGENTS.md`, `.agents/skills/**`.
- Do not package, install, or copy them into `src/auto_loop`, wheels, or `auto-loop init` output.
- Target repos may have their own `AGENTS.md`; that is unrelated to this harness.

## Verification

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m pytest tests/unit/test_packaging.py tests/unit/test_development_harness.py -q
```

Live Cursor smoke is opt-in (`AUTO_LOOP_LIVE_CURSOR=1`); do not claim it passed unless you ran it.
