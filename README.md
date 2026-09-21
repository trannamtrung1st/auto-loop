# auto-loop

**auto-loop** is a Python controller for a planner / worker / reviewer lifecycle. A **planner** writes the initial plan, a **plan reviewer** approves it, a **worker** implements scoped batches (and may update the plan), and an **execution reviewer** is the sole completion authority. Git commits are the source of truth for product progress; durable state under `.auto-loop/` records sessions, reviews, and recovery metadata.

This document describes the current operator experience. Parallel workers, PR bots, dashboards, and similar ideas remain out of scope.

## Requirements

- **Python 3.12+**
- A **Git** repository for the product code you want the loop to change
- For live runs: the **Cursor agent CLI** (`agent` or `cursor-agent` on `PATH`, configurable in `.auto-loop/config.yaml`) and valid Cursor credentials. The offline test suite does not call live Cursor.

## Installation

From a built wheel (recommended for a clean environment):

```bash
python -m pip install auto-loop-*.whl
auto-loop --version
```

From a source checkout (development):

```bash
python -m pip install -e ".[dev]"
```

Verify packaging without importing the source tree:

```bash
python -m pytest tests/unit/test_packaging.py -q
```

## Quickstart

```bash
cd your-product-repo
git init   # if needed
auto-loop init
# Edit .auto-loop/task.md and .auto-loop/plan.md
auto-loop doctor
auto-loop run --max-turns 20
auto-loop status
```

`init` creates `.auto-loop/` with `config.yaml`, `context.yaml`, planner/worker/reviewer role files, instructions, review directory, and optional resource stubs. Re-running `init` is non-destructive unless you pass `--force`. Use `--minimal` for a skeleton without instructions/resources.

## Commands

| Command | Purpose |
|--------|---------|
| `auto-loop init [path]` | Create control workspace templates (`--force`, `--minimal`). |
| `auto-loop doctor [path]` | Check workspace layout, config, Git, Cursor CLI, sessions, lock. |
| `auto-loop run [path]` | Start or continue the lifecycle (`--max-turns`, `--max-runtime-minutes`, `--planner-model` / `--worker-model` / `--reviewer-model` / `--model`, `--verbose` / `--quiet`). |
| `auto-loop status [path]` | Summarize lifecycle id, phase, turn, baseline, HEAD, sessions, plan approval. |
| `auto-loop logs [path]` | Show per-turn provider logs (`--turn`, `--raw`, `--follow`). |
| `auto-loop stop [path]` | Request graceful stop of the active run (also respects SIGINT/SIGTERM during `run`). |

All commands accept an optional repository path; default is the current working directory.

## Stable exit codes

| Code | Name | Meaning |
|-----:|------|---------|
| 0 | `COMPLETE` | Whole task accepted (final reviewer `COMPLETE`). |
| 2 | `BLOCKED` | Reviewer or worker declared a genuine external blocker. |
| 3 | `LIMIT_REACHED` | `max_turns`, `max_runtime_minutes`, or no-progress limit hit. |
| 4 | `STOPPED` | Operator stop (CLI or signal). |
| 10 | `CONFIG_ERROR` | Missing/invalid workspace or configuration. |
| 11 | `PROVIDER_ERROR` | Cursor/provider infrastructure failure after retries. |
| 12 | `PROTOCOL_ERROR` | Agent output missing/invalid `AUTO_LOOP_RESULT` after repair budget. |
| 13 | `PROTECTION_VIOLATION` | Protected control file changed during a turn (not auto-reverted). |
| 14 | `CONCURRENT_RUN` | Another lifecycle holds the workspace lock. |
| 15 | `GIT_PROTOCOL_ERROR` | Git/review-range invariant violated. |
| 16 | `SESSION_ERROR` | Resume returned an unexpected session id (identity is not rotated). |
| 17 | `REVIEW_MUTATION_ERROR` | Reviewer changed product repository state. |
| 18 | `INTERNAL_ERROR` | Unexpected controller failure. |

## Lifecycle and Git invariants

1. **Plan first** — Planner and plan-reviewer sessions run before implementation. Product HEAD stays at the initial baseline until initial plan `PASS`. Those planning sessions then retire.
2. **Execution sessions** — Worker and execution reviewer start fresh after plan PASS. The worker owns `plan.md` and may update it. Task.md stays authoritative.
3. **Batch reviews** — The controller normalizes Git review to `last_approved_commit..HEAD`. Explicit ignored path targets can be reviewed with or without a Git range. Cumulative revision reviews use the full current candidate, so amending an unapproved review-fix commit is allowed.
4. **Baseline** — Batch `PASS` advances `last_approved_commit` only when a non-empty Git candidate was reviewed. Path-only PASS does not move the baseline.
5. **Final** — Worker requests `scope: final` only when HEAD equals `last_approved_commit` and the tree is clean; only the execution reviewer may return `COMPLETE`.
6. **Reviews** — Markdown artifacts under `.auto-loop/reviews/` are append-only evidence, including review cycle/round and target fingerprints.

## Persistent sessions and recovery

- Four session slots exist: `planner`, `plan_reviewer`, `worker`, `reviewer`. Planning slots retire after initial plan PASS and are not resumed.
- Session mismatch on resume exits with **`SESSION_ERROR`**; the controller does not silently replace ids.
- Resolved model is stored per session. CLI model overrides apply when creating a session, not to an existing one.
- **Inflight** markers detect interrupted turns; the next `run` resumes the same session slot with a reconciliation prompt.
- **Protocol repair** re-prompts the same session when work succeeded but `AUTO_LOOP_RESULT` was missing/invalid, within `limits.protocol_retries`.
- **Provider retries** (`limits.provider_retries`) retry infrastructure failures without rotating session ids.

## Instruction layering

Generated defaults live under `.auto-loop/instructions/` and `.auto-loop/agents/`. Config key `instructions.*.mode` is `extend` (default) or `replace_role`. Shared protocol text is composed with role-specific instructions each first session turn. Customize planner/worker/reviewer behavior by editing those files or pointing `instructions.*.files` at your own markdown.

## Context and task resources

`context.yaml` lists optional resources (paths, skills) included in prompts per role (`shared`, `planner`, `worker`, `reviewer`). Plan-reviewer receives shared + reviewer resources. Validate with `doctor`. Task-specific material belongs in `.auto-loop/task.md` and `.auto-loop/plan.md`; treat `task.md` as authoritative for scope.

Recommended protected control inputs (configure in `protection.protected_files`):

```yaml
protection:
  protected_files:
    - .auto-loop/task.md
```

Mutations during a turn stop the run with **`PROTECTION_VIOLATION`**; auto-loop does not revert files for you.

## Developing auto-loop

Contributors working on this repository should read root [`AGENTS.md`](AGENTS.md) and the skills under [`.agents/skills/`](.agents/skills/). Cursor discovers them by opening the repo; there is no CLI install step. Those files are development guidance for auto-loop itself, not runtime resources copied into target task repositories.

## Observability

- **Events** — `.auto-loop/runtime/events.jsonl` (lifecycle started, reviews, baseline advanced, stops, limits).
- **State** — `.auto-loop/runtime/state.json` (phase, turn, next session, four session slots, active review, pending revision, inflight).
- **Turn logs** — `.auto-loop/runtime/runs/<lifecycle_id>/` per-turn `.jsonl` / `.log` streams, named by session purpose.
- **Completion / blocked** — `.auto-loop/runtime/completion.json` or `blocked.json` when terminal.

Use `auto-loop status` and `auto-loop logs` for operator-friendly views.

## Stop, restart, and limits

- `auto-loop stop` marks a running lifecycle **STOPPED** when a controller is active.
- Re-run `auto-loop run` to continue from durable state (same session ids, reconciled inflight).
- Defaults: `limits.max_turns`, `limits.max_runtime_minutes`, `limits.max_consecutive_worker_no_progress`—tune in `config.yaml` or `run` flags.

## Configuration reference (high level)

Key sections in `.auto-loop/config.yaml`:

| Section | Role |
|---------|------|
| `provider.cursor` | CLI command (`agent` / `cursor-agent`), extra args per role. |
| `agents.planner` / `agents.worker` / `agents.reviewer` | Role files, model, `agent` vs `ask` mode. Plan-reviewer uses the reviewer model. |
| `instructions` | Shared/planner/worker/reviewer markdown stacks. |
| `git` | Clean-tree requirements, worker commits, history protection. |
| `limits` | Turns, runtime, timeouts, retries, no-progress streak. |
| `protection` | Product exclude globs, protected control files. |
| `logging` | Console verbosity, event log path, run history retention. |

Model precedence for each role: role-specific CLI override, then `--model`, then `config.agents.<role>.model`, then `auto`.

See the generated file after `init` for defaults.

## Troubleshooting

| Symptom | Things to check |
|---------|------------------|
| `CONFIG_ERROR` on `run` | Run `auto-loop init` and `doctor`; fix `context.yaml` / missing paths. Missing planner templates: rerun `auto-loop init`. |
| `GIT_PROTOCOL_ERROR` | Dirty product tree before batch review, empty Git range without path targets, or history rewrite. |
| `SESSION_ERROR` | Cursor resume id drift; inspect turn logs; do not hand-edit session ids in `state.json`. |
| `PROTOCOL_ERROR` | Agent forgot `AUTO_LOOP_RESULT`; increase `protocol_retries` only after fixing prompts. |
| `LIMIT_REACHED` | Raise `max_turns` / runtime or reduce revise loops; check `worker_no_progress_streak`. |
| `CONCURRENT_RUN` | Another `run` holds `.auto-loop/runtime/lock.json`; wait, use `auto-loop stop`, or remove a stale lock after verifying no live controller. |
| Doctor: Cursor not found | Install Cursor CLI or set `provider.cursor.command`. |

## Security and secrets

- Do not commit API keys or `.env` with credentials.
- Cursor authentication is handled by the Cursor CLI environment, not stored in `.auto-loop/`.
- Review artifacts and logs may contain repository paths and agent text—treat them as sensitive if your task is.

## Testing

Offline verification (no live Cursor):

```bash
python -m pytest -q
```

Integration tests use a deterministic in-process provider and real temporary Git repositories. The CLI `run` command invokes the **real Cursor agent subprocess** when prerequisites are met.

Optional live Cursor smoke test: see [docs/live-cursor-smoke.md](docs/live-cursor-smoke.md). Set `AUTO_LOOP_LIVE_CURSOR=1` and run `pytest tests/integration/test_live_cursor_smoke.py -m live_cursor`. Skipped by default with an explicit reason.

## Traceability and release verification

- [docs/v1-traceability.md](docs/v1-traceability.md) — requirement mapping to code and tests.
- [docs/v1-verification.md](docs/v1-verification.md) — recorded commands, results, and live-smoke status.

## License

MIT — see [LICENSE](LICENSE).
