# auto-loop

**auto-loop** is a Python controller for a two-role implementation/review lifecycle: a **worker** agent implements scoped batches and a **reviewer** agent approves plan, batch, and final acceptance. Git commits are the source of truth for product progress; durable state under `.auto-loop/` records sessions, reviews, and recovery metadata.

This document describes the **v1 operator experience** (proposal sections 1–48). Section 49 ideas (parallel workers, PR bots, dashboards, and similar) are **not** part of v1.

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

`init` creates `.auto-loop/` with `config.yaml`, `context.yaml`, role agents, instructions, review directory, and optional resource stubs. Re-running `init` is non-destructive unless you pass `--force`. Use `--minimal` for a skeleton without instructions/resources.

## Commands

| Command | Purpose |
|--------|---------|
| `auto-loop init [path]` | Create control workspace templates (`--force`, `--minimal`). |
| `auto-loop doctor [path]` | Check workspace layout, config, Git, Cursor CLI, sessions, lock. |
| `auto-loop run [path]` | Start or continue the worker/reviewer loop (`--max-turns`, `--max-runtime-minutes`, model overrides, `--verbose` / `--quiet`). |
| `auto-loop status [path]` | Summarize lifecycle id, turn, baseline, HEAD, plan approval, active review. |
| `auto-loop logs [path]` | Show per-turn provider logs (`--turn`, `--raw`, `--follow`). |
| `auto-loop stop [path]` | Request graceful stop of the active run (also respects SIGINT/SIGTERM during `run`). |
| `auto-loop resources install [path]` | Install bundled AGENTS.md and Agent Skills (`--profile core\|frontend`, `--dry-run`, `--no-agents-md`). |

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

1. **Plan first** — No product commits before plan review `PASS`; HEAD stays at the initial baseline until then.
2. **Batch reviews** — Worker requests review for a commit range; the controller normalizes to `last_approved_commit..HEAD`. Cumulative revision batches review from the approved baseline through the current HEAD, not only the latest fix commit.
3. **Baseline** — Batch `PASS` advances `last_approved_commit` to current HEAD.
4. **Final** — Worker requests `scope: final` only when HEAD equals `last_approved_commit` and the tree is clean; only the reviewer may return `COMPLETE`.
5. **Reviews** — Markdown artifacts under `.auto-loop/reviews/` are append-only evidence.

## Persistent sessions and recovery

- Worker and reviewer each keep a **stable Cursor session id** across turns until the lifecycle ends.
- Session mismatch on resume exits with **`SESSION_ERROR`**; the controller does not silently replace ids.
- **Inflight** markers detect interrupted turns; the next `run` resumes the same role with a reconciliation prompt (inspect Git/state before duplicating work).
- **Protocol repair** re-prompts the same session when work succeeded but `AUTO_LOOP_RESULT` was missing/invalid, within `limits.protocol_retries`.
- **Provider retries** (`limits.provider_retries`) retry infrastructure failures without rotating session ids.

## Instruction layering

Generated defaults live under `.auto-loop/instructions/` and `.auto-loop/agents/`. Config key `instructions.*.mode` is `extend` (default) or `replace_role`. Shared protocol text is composed with role-specific instructions each turn. Customize worker/reviewer behavior by editing those files or pointing `instructions.*.files` at your own markdown.

## Context and task resources

`context.yaml` lists optional resources (paths, skills) included in prompts per role. Validate with `doctor`. Task-specific material belongs in `.auto-loop/task.md` and `.auto-loop/plan.md`; treat `task.md` as authoritative for scope.

Recommended protected control inputs (configure in `protection.protected_files`):

```yaml
protection:
  protected_files:
    - .auto-loop/task.md
```

Mutations during a turn stop the run with **`PROTECTION_VIOLATION`**; auto-loop does not revert files for you.

## Optional harness resources (Cursor / Codex)

Bundled cross-agent files ship inside the wheel under `auto_loop.harness_resources`:

```bash
auto-loop resources install --profile core      # repo-discovery, implementation-batch, test-and-verify, …
auto-loop resources install --profile frontend  # core skills + ui-validation
auto-loop resources install --dry-run           # preview CREATE/SKIP actions
```

This installs repository-root `AGENTS.md` and `.agents/skills/*/SKILL.md` when missing. Existing files are skipped unless you manage them manually—use `--dry-run` to preview conflicts.

## Observability

- **Events** — `.auto-loop/runtime/events.jsonl` (lifecycle started, reviews, baseline advanced, stops, limits).
- **State** — `.auto-loop/runtime/state.json` (turn, next actor, sessions, active review, inflight).
- **Turn logs** — `.auto-loop/runtime/runs/<lifecycle_id>/` per-turn `.jsonl` / `.log` streams.
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
| `agents.worker` / `agents.reviewer` | Role files, model, `agent` vs `ask` mode. |
| `instructions` | Shared/worker/reviewer markdown stacks. |
| `git` | Clean-tree requirements, worker commits, history protection. |
| `limits` | Turns, runtime, timeouts, retries, no-progress streak. |
| `protection` | Product exclude globs, protected control files. |
| `logging` | Console verbosity, event log path, run history retention. |

See the generated file after `init` for defaults.

## Troubleshooting

| Symptom | Things to check |
|---------|------------------|
| `CONFIG_ERROR` on `run` | Run `auto-loop init` and `doctor`; fix `context.yaml` / missing paths. |
| `GIT_PROTOCOL_ERROR` | Dirty product tree before batch review, wrong `base_commit`/`head_commit`, or history rewrite. |
| `SESSION_ERROR` | Cursor resume id drift; inspect turn logs; do not hand-edit session ids in `state.json`. |
| `PROTOCOL_ERROR` | Agent forgot `AUTO_LOOP_RESULT`; increase `protocol_retries` only after fixing prompts. |
| `LIMIT_REACHED` | Raise `max_turns` / runtime or reduce revise loops; check `worker_no_progress_streak`. |
| `CONCURRENT_RUN` | Another `run` holds `.auto-loop/runtime/workspace.lock`; wait or stop the other process. |
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

Optional live Cursor smoke test (proposal §45): see [docs/live-cursor-smoke.md](docs/live-cursor-smoke.md). Set `AUTO_LOOP_LIVE_CURSOR=1` and run `pytest tests/integration/test_live_cursor_smoke.py -m live_cursor`. Skipped by default with an explicit reason.

## License

MIT — see [LICENSE](LICENSE).
