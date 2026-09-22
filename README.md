# auto-loop

**auto-loop** is a Python controller for a planner / worker / reviewer lifecycle. A **planner** writes the initial plan, a **plan reviewer** approves it, a **worker** implements scoped batches (and may update the plan), and an **execution reviewer** is the sole completion authority. Review evidence is the source of truth for approval. Git ranges are used when they exist and the run's Git mode asks for them.

The operator contract is:

> **one explicit run manifest + one task document + one command**

Auto Loop writes all generated plan, review, and runtime state under the configured artifact root. You do not edit that directory for normal use.

This document describes the current operator experience. Parallel workers, PR bots, dashboards, and similar ideas remain out of scope.

## Requirements

- **Python 3.12+**
- **Git** is optional. `git.mode: required` needs a repository; `optional` uses one when present; `off` reviews explicit targets in a plain directory
- For live runs: the **Cursor agent CLI** (`agent` or `cursor-agent` on `PATH`, configurable in the run YAML) and valid Cursor credentials. The offline test suite does not call live Cursor.

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

Create two files. A typical layout:

```text
my-product/
├── src/
├── tests/
└── .ai/
    ├── run.yaml
    └── proposal.md
```

`.ai/run.yaml`:

```yaml
version: 2
workspace: ..

task:
  source: .ai/proposal.md

artifacts:
  root: .ai/auto-loop

models:
  planner: auto
  worker: auto
  reviewer: auto

run:
  max_turns: 100
  max_runtime_minutes: 480
```

Then:

```bash
cd my-product
auto-loop run .ai/run.yaml
auto-loop status .ai/run.yaml
```

`init` is optional. It only writes a starter run YAML; it does not create a task, runtime state, or generated agent files. Package defaults are enough to start.

For every supported public setting with defaults and comments:

```bash
auto-loop init .ai/run.yaml --full
```

See also the packaged reference template (`run.full.yaml` inside the wheel) and the section [Full configuration example](#full-configuration-example) below.

A maintained example is [`samples/kanban-board`](samples/kanban-board):

```bash
cd samples/kanban-board
auto-loop doctor .ai/run.yaml
auto-loop run .ai/run.yaml
```

## Three essential settings

| Setting | Meaning |
|---------|---------|
| `workspace` | Repository Auto Loop is allowed to modify. Relative values are resolved from the directory that contains the run YAML. |
| `task.source` | User-owned task/goal file. Relative values are resolved from `workspace`. |
| `artifacts.root` | Where Auto Loop stores plan, reviews, and runtime state. Must stay inside `workspace`. |

All other paths in the run YAML (`task.source`, `artifacts.root`, context resources, custom instruction files, protected paths) are relative to `workspace` unless they are absolute. Moving the YAML file does not silently change repository paths.

The filename and location of the run YAML are arbitrary:

```bash
auto-loop run .ai/run.yaml
auto-loop run .local/feature-123.yml
auto-loop run /home/me/loop-configs/product-a.yaml
```

There is no implicit discovery of a root `auto-loop.yaml`, `context.yaml`, `goal.md`, or `task.md`.

## Commands

| Command | Purpose |
|--------|---------|
| `auto-loop run RUN_CONFIG` | Start a new lifecycle from the explicit run YAML. Rejects if a run is already active. |
| `auto-loop resume RUN_CONFIG` | Resume the stored run using the frozen snapshot under the artifact root. |
| `auto-loop status RUN_CONFIG` | Summarize run, phase, planner/worker/reviewer, and where to inspect the plan/reviews. |
| `auto-loop doctor RUN_CONFIG` | Check the manifest, workspace, Git, task source, context, instructions, Cursor CLI, lock, and current run state. |
| `auto-loop logs RUN_CONFIG` | Show per-turn provider logs (`--turn`, `--raw`, `--follow`). |
| `auto-loop stop RUN_CONFIG` | Request graceful stop of the active run (also respects SIGINT/SIGTERM during `run`). |
| `auto-loop init PATH` | Optional starter YAML only (`--force` to overwrite). Not required to run. |

Operational flags such as `--verbose` / `--quiet` and `logs --follow` remain. Model, limit, goal, and context settings belong in the run YAML, not duplicated as `run` flags.

## User-owned vs tool-managed

| Path | Owner | User edits normally? |
|------|--------|---------------------:|
| run YAML (any name) | you | yes — the one manifest you pass to the CLI |
| task/proposal file | you | yes — referenced by `task.source` |
| optional context resources / custom instruction files | you | yes — listed in the run YAML |
| `<artifacts.root>/` | Auto Loop | no — tool-managed plan, reviews, runtime |

On a new run Auto Loop copies the task source into `<artifacts.root>/task.md` and freezes a resolved snapshot at `<artifacts.root>/runtime/config.resolved.yaml`. Editing the source YAML or proposal does not change an **active** lifecycle. After a **terminal** run, `auto-loop run RUN_CONFIG` again archives the prior artifacts and starts a fresh lifecycle from the current YAML and task file.

## Full configuration example

Auto Loop v2 uses **one public field per setting** in the run YAML:

- **`models.*`** — planner, worker, and reviewer model selection (do not duplicate under `agents.*.model`).
- **`run.*`** — turns, runtime, timeouts, retries, and no-progress limits (do not use a separate top-level `limits` section).

Generate a fully-commented reference manifest in your repo:

```bash
auto-loop init .ai/run.yaml --full
```

The same template ships inside the package as `auto_loop/templates/run.full.yaml`. The concise starter is `run.yaml` (what default `auto-loop init` writes). [`samples/kanban-board/.ai/run.yaml`](samples/kanban-board/.ai/run.yaml) shows a readable real-world manifest without listing every optional section.

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
| 15 | `GIT_PROTOCOL_ERROR` | Review evidence or Git-mode invariant violated. |
| 16 | `SESSION_ERROR` | Resume returned an unexpected session id (identity is not rotated). |
| 17 | `REVIEW_MUTATION_ERROR` | Reviewer changed product repository state. |
| 18 | `INTERNAL_ERROR` | Unexpected controller failure. |

## Lifecycle and review invariants

1. **Plan first** — Planner and plan-reviewer sessions run before implementation. Those planning sessions then retire. In `git.mode: required`, product HEAD stays at the initial baseline and the product tree stays clean until plan `PASS`. In `optional` or `off`, pre-existing dirt does not block plan review; the planner still must not mutate product files during the turn.
2. **Execution sessions** — Worker and execution reviewer start fresh after plan PASS. The worker owns `plan.md` and may update it. The task snapshot stays authoritative.
3. **Reviews approve evidence** — A review can include a Git range, workspace path targets (tracked, untracked, or ignored), and inline content. An empty Git range is valid when other targets exist. In `git.mode: required`, product changes are reviewed as `last_approved_commit..HEAD` on a clean tree.
4. **Baseline** — Batch `PASS` advances the approved Git head only when a Git range was reviewed. Path-only or content-only PASS records that target's fingerprint and does not synthesize a commit.
5. **Final** — Only the execution reviewer may return `COMPLETE`. In `git.mode: required`, HEAD must equal the approved commit and the product tree must be clean. In `optional` or `off`, completion is the reviewed final evidence, with no commit required.
6. **Reviews** — Markdown artifacts under `<artifacts.root>/reviews/` are append-only evidence, including review cycle/round and target fingerprints.

## Persistent sessions and recovery

- Four session slots exist: `planner`, `plan_reviewer`, `worker`, `reviewer`. Planning slots retire after initial plan PASS and are not resumed.
- Session mismatch on resume exits with **`SESSION_ERROR`**; the controller does not silently replace ids.
- Resolved model is stored per session. Changing models in the source YAML applies only to a future lifecycle.
- **Inflight** markers detect interrupted turns; the next `resume` continues the same session slot with a reconciliation prompt.
- **Protocol repair** re-prompts the same session when work succeeded but `AUTO_LOOP_RESULT` was missing/invalid, within `run.protocol_retries`.
- **Provider retries** (`run.provider_retries`) retry infrastructure failures without rotating session ids.

## Instruction layering

Default role prompts and instruction stacks ship in the package. A fresh install can run with only the run YAML and a proposal.

Config key `instructions.*.mode` is `extend` (default) or `replace_role`. Shared protocol text is composed with role-specific instructions each first session turn. Point `instructions.*.files` at your own markdown when you want extra guidance:

```yaml
instructions:
  worker:
    files:
      - .ai/instructions/worker.md
```

## Context and task resources

Optional context lives in the run YAML. There is no standalone user `context.yaml`.

```yaml
context:
  shared:
    resources:
      - README.md
      - docs/architecture.md
  worker:
    resources:
      - src/
      - tests/
  reviewer:
    resources:
      - tests/
```

Plan-reviewer receives shared + reviewer resources. Validate with `doctor`. The run **task source** is the user-facing intent; internally it is snapshotted for the planner/worker/reviewer protocol.

If the run YAML or task source is inside the workspace, Auto Loop treats those paths as protected during agent turns. Mutations stop the run with **`PROTECTION_VIOLATION`**; auto-loop does not revert files for you.

The artifact root is excluded from product cleanliness and worker production-change calculations even if it is not gitignored. `doctor` warns when an in-repo artifact root is not listed in `.gitignore`.

## Developing auto-loop

Contributors working on this repository should read root [`AGENTS.md`](AGENTS.md) and the skills under [`.agents/skills/`](.agents/skills/). Cursor discovers them by opening the repo; there is no CLI install step. Those files are development guidance for auto-loop itself, not runtime resources copied into target task repositories.

## Observability

- **Events** — `<artifacts.root>/runtime/events.jsonl` (lifecycle started, reviews, baseline advanced, stops, limits).
- **State** — `<artifacts.root>/runtime/state.json` (phase, turn, next session, four session slots, active review, pending revision, inflight).
- **Turn logs** — `<artifacts.root>/runtime/runs/<lifecycle_id>/` per-turn streams, named by session purpose. `.jsonl` is the raw provider NDJSON, appended as each line arrives. `.log` is the normalized thinking / message / tool trace, also appended immediately. `auto-loop logs RUN_CONFIG --follow` tails that activity while the agent is running. `--raw` prints the JSONL unchanged.
- **Run console** — normal and verbose runs show that trace live in the same terminal: thinking, assistant text, and tool start/end, as each event arrives. Assistant text is shown once. Live deltas render immediately; a buffered assistant copy is shown only when those deltas were absent, and a repeated copy is skipped. `--quiet` hides the live trace and still writes the turn logs. `--verbose` keeps the trace and adds session and lifecycle diagnostics, with longer tool-argument excerpts. Tool results stay summarized; the full payload remains in the JSONL. `auto-loop logs --follow` tails the same trace from the turn log; it is not required to watch a run.
- **Completion / blocked** — `<artifacts.root>/runtime/completion.json` or `blocked.json` when terminal.

Use `auto-loop status RUN_CONFIG` and `auto-loop logs RUN_CONFIG` for operator-friendly views.

## Stop, restart, and limits

- `auto-loop stop RUN_CONFIG` signals a live controller. The first request is graceful. If that controller does not exit, stop force-kills the verified provider and then the controller. A dead controller does not count as stopped while its recorded provider is still the process Auto Loop launched: stop terminates that process, clears `active_run.json` and the workspace lock, and persists **STOPPED**. The inflight marker is kept.
- Ctrl+C requests the same graceful stop (`Stopping active agent…`). A second Ctrl+C force-stops the provider (`Force stopping…`). The signal handler only sets that request and sends a non-blocking signal. Provider supervision performs the wait, persists **STOPPED**, and releases ownership.
- `auto-loop run` and `auto-loop resume` reconcile a dead controller before taking the workspace lock, so a stale lock or a reused PID is not reported as a concurrent run. A PID from `active_run.json` is killed only when its start time still matches the recorded process.
- `auto-loop resume RUN_CONFIG` continues from durable state (same session ids, reconciled inflight). Changing the source proposal does not replace an in-progress run.
- After a **terminal** run (`completion.json` or `blocked.json`), `auto-loop run RUN_CONFIG` again archives the prior run’s plan, task snapshot, reviews, and runtime notes under `<artifacts.root>/runtime/archives/<lifecycle-id>/` using each file’s workspace-relative path. Active (non-terminal) runs must be resumed, not replaced. `auto-loop resume` on a blocked terminal run exits with `BLOCKED` and the saved summary rather than re-entering the loop.
- Defaults: `run.max_turns`, `run.max_runtime_minutes`, `run.max_consecutive_worker_no_progress` — tune them in the run YAML.

## Configuration reference (high level)

Version 2 is the supported public schema. Version 1 files fail with a clear error; there is no silent translation or `migrate` command.

| Section | Role |
|---------|------|
| `workspace` | Product repository; the only path resolved relative to the YAML file. |
| `task.source` | User-owned task/goal file. |
| `artifacts.root` | Tool-managed output tree (`task.md`, `plan.md`, `reviews/`, `runtime/`). |
| `models.planner` / `models.worker` / `models.reviewer` | Per-role model ids (canonical; plan-reviewer uses `models.reviewer`). |
| `run.*` | Turns, runtime, agent timeouts, provider/protocol retries, no-progress streak (canonical). |
| `provider.cursor` | CLI command (`agent` / `cursor-agent`), extra args per role. |
| `agents.planner` / `agents.worker` / `agents.reviewer` | Optional custom role files and `agent` vs `ask` mode only (not model selection). |
| `instructions` | Optional shared/planner/worker/reviewer markdown stacks. |
| `context` | Optional per-role resource and skill lists (`path`, `purpose`, `required`). |
| `git` | `mode` (`required`, `optional`, or `off`) and approved-history protection. |
| `protection` | Extra product exclude globs and protected control files. |
| `logging` | Console verbosity and run history retention. |

Generate a commented manifest with every public section: `auto-loop init PATH --full` (see [Full configuration example](#full-configuration-example)).

## Troubleshooting

| Symptom | Things to check |
|---------|------------------|
| `CONFIG_ERROR` on `run` | Pass an explicit version 2 YAML; `doctor RUN_CONFIG`; confirm `task.source` exists and is non-empty. |
| `GIT_PROTOCOL_ERROR` | Strict mode saw a dirty tree or missing repository, a review had no evidence, or approved history was rewritten. |
| `SESSION_ERROR` | Cursor resume id drift; inspect turn logs; do not hand-edit session ids in `state.json`. |
| `PROTOCOL_ERROR` | Agent forgot `AUTO_LOOP_RESULT`; increase `run.protocol_retries` only after fixing prompts. |
| `LIMIT_REACHED` | Raise `max_turns` / runtime or reduce revise loops; check `worker_no_progress_streak`. |
| `CONCURRENT_RUN` | Another live controller holds `<artifacts.root>/runtime/lock.json`. Wait, or run `auto-loop stop`. A dead controller's lock is cleared on the next `run`, `resume`, `stop`, or `doctor`. |
| Doctor: Cursor not found | Install Cursor CLI or set `provider.cursor.command`. |

## Security and secrets

- Do not commit API keys or `.env` with credentials.
- Cursor authentication is handled by the Cursor CLI environment, not stored under the artifact root.
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
