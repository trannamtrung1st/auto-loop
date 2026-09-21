# auto-loop

**auto-loop** is a Python controller for a planner / worker / reviewer lifecycle. A **planner** writes the initial plan, a **plan reviewer** approves it, a **worker** implements scoped batches (and may update the plan), and an **execution reviewer** is the sole completion authority. Git commits are the source of truth for product progress. **`.auto-loop/` is tool-managed state** (plan, reviews, sessions, runtime); you do not edit it for normal use.

This document describes the current operator experience. Parallel workers, PR bots, dashboards, and similar ideas remain out of scope.

## Requirements

- **Python 3.12+**
- A **Git** repository for the product code you want the loop to change
- For live runs: the **Cursor agent CLI** (`agent` or `cursor-agent` on `PATH`, configurable in `auto-loop.yaml`) and valid Cursor credentials. The offline test suite does not call live Cursor.

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
auto-loop run "Implement the feature described in README.md"
auto-loop status
```

Or pass a goal file:

```bash
auto-loop run --goal-file goal.md
```

`init` creates user-owned `auto-loop.yaml` and prints how to start a run (including optional `.auto-loop/runtime/` ignore guidance). It does not create a goal, a root `task.md`, or a root `context.yaml`. `.auto-loop/` is created when a run starts. Re-running `init` is non-destructive unless you pass `--force`. Use `--minimal` for a skeleton without default instruction files.

A maintained example is [`samples/kanban-board`](samples/kanban-board):

```bash
./bootstrap.sh
auto-loop run --goal-file goal.md
```

## Commands

| Command | Purpose |
|--------|---------|
| `auto-loop init [path]` | Create `auto-loop.yaml` (`--force`, `--minimal`). |
| `auto-loop run ["goal"]` | Start a run from inline goal or `--goal-file` (`--context`, `--path`, `--max-turns`, `--max-runtime-minutes`, `--planner-model` / `--worker-model` / `--reviewer-model` / `--model`, `--verbose` / `--quiet`). Omit a new goal to continue the stored run. |
| `auto-loop resume [path]` | Resume the stored run without replacing its goal. |
| `auto-loop status [path]` | Summarize run, phase, planner/worker/reviewer, and where to inspect the plan/reviews. |
| `auto-loop doctor [path]` | Check config, Git, Cursor CLI, sessions, lock. |
| `auto-loop migrate [path]` | Create `auto-loop.yaml` from a legacy layout without discarding run state. |
| `auto-loop logs [path]` | Show per-turn provider logs (`--turn`, `--raw`, `--follow`). |
| `auto-loop stop [path]` | Request graceful stop of the active run (also respects SIGINT/SIGTERM during `run`). |

`run` accepts `--path` for the target repository. Other commands still take an optional path argument; default is the current working directory.

## User-owned vs tool-managed

| Path | Owner | User edits normally? |
|------|--------|---------------------:|
| `auto-loop.yaml` | you | yes — config |
| `goal.md` | you | yes — optional goal input |
| custom context/resource files | you | yes — optional, pass `--context` |
| `.auto-loop/` | Auto Loop | no — tool-managed plan, reviews, agents, runtime |

Configuration resolution: built-in defaults plus `auto-loop.yaml` when present. Repositories without `auto-loop.yaml` still load legacy `.auto-loop/config.yaml`. On `auto-loop run`, the resolved snapshot is frozen under `.auto-loop/runtime/config.resolved.yaml` for resume; CLI overrides apply at launch.

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

Context is optional. Pass `--context path/to/context.yaml` when a run needs extra resources or skills. Auto Loop snapshots that file into tool-managed state; a root `context.yaml` is not required.

The context document lists optional resources (paths, skills) included in prompts per role (`shared`, `planner`, `worker`, `reviewer`). Plan-reviewer receives shared + reviewer resources. Validate with `doctor`. The run **goal** is the user-facing source of intent; internally it is snapshotted for the planner/worker/reviewer protocol.

Recommended protected control inputs (configure in `protection.protected_files`):

```yaml
protection:
  protected_files:
    - auto-loop.yaml
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
- `auto-loop resume` (or `auto-loop run` with no new goal) continues from durable state (same session ids, reconciled inflight). Changing `goal.md` does not replace an in-progress run.
- After a **terminal** run (`completion.json` or `blocked.json`), supplying a **new goal** archives the prior run’s plan, context, reviews, and runtime notes under `.auto-loop/runtime/archives/<lifecycle-id>/`, then regenerates default **plan** and **context** only (custom agent/instruction files are preserved). Active (non-terminal) runs must be resumed, not replaced. `auto-loop resume` (or `run` with no new goal) on a blocked terminal run exits with `BLOCKED` and the saved summary rather than re-entering the loop.
- Defaults: `limits.max_turns`, `limits.max_runtime_minutes`, `limits.max_consecutive_worker_no_progress`—tune in `auto-loop.yaml` or `run` flags.

## Configuration reference (high level)

User-owned config is `auto-loop.yaml`. A compact file is enough:

```yaml
models:
  planner: auto
  worker: auto
  reviewer: auto

run:
  max_turns: 100
  max_runtime_minutes: 480
```

The full internal schema is still accepted in `auto-loop.yaml` (and used for the tool-managed snapshot):

| Section | Role |
|---------|------|
| `models` / `agents.*.model` | Per-role model ids. Plan-reviewer uses the reviewer model. |
| `run.max_turns` / `run.max_runtime_minutes` | Convenience aliases for `limits`. |
| `provider.cursor` | CLI command (`agent` / `cursor-agent`), extra args per role. |
| `agents.planner` / `agents.worker` / `agents.reviewer` | Role files, model, `agent` vs `ask` mode. |
| `instructions` | Shared/planner/worker/reviewer markdown stacks. |
| `git` | Clean-tree requirements, worker commits, history protection. |
| `limits` | Turns, runtime, timeouts, retries, no-progress streak. |
| `protection` | Product exclude globs, protected control files. |
| `logging` | Console verbosity, event log path, run history retention. |

Model precedence for each role: role-specific CLI override, then `--model`, then `auto-loop.yaml` / `agents.<role>.model`, then `auto`.

## Troubleshooting

| Symptom | Things to check |
|---------|------------------|
| `CONFIG_ERROR` on `run` | Run `auto-loop init` and `doctor`; provide a goal (`auto-loop run "…"` or `--goal-file`). Optional `--context` if you use extra resources. Missing planner templates: rerun `auto-loop init` then `run`. |
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

GitHub Actions (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main`: offline pytest on Python 3.12 and 3.14, packaging tests plus a wheel build, and `ruff check src tests`. It does not set `AUTO_LOOP_LIVE_CURSOR` and does not need Cursor credentials or repository secrets.

The same checks locally:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m pytest tests/unit/test_packaging.py -q
python -m pip wheel . --no-deps -w dist
ruff check src tests
```

Integration tests use a deterministic in-process provider and real temporary Git repositories. The CLI `run` command invokes the **real Cursor agent subprocess** when prerequisites are met.

Optional live Cursor smoke test: see [docs/live-cursor-smoke.md](docs/live-cursor-smoke.md). Set `AUTO_LOOP_LIVE_CURSOR=1` and run `pytest tests/integration/test_live_cursor_smoke.py -m live_cursor` on a machine with Cursor CLI auth. Skipped by default (including in GitHub CI) with an explicit reason.

## Traceability and release verification

- [docs/v1-traceability.md](docs/v1-traceability.md) — requirement mapping to code and tests.
- [docs/v1-verification.md](docs/v1-verification.md) — recorded commands, results, and live-smoke status.

## License

MIT — see [LICENSE](LICENSE).
