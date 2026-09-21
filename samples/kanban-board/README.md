# Kanban board sample

A clean Auto Loop project: a user-owned goal and config, with no checked-in `.auto-loop/` state.

## Quick start

From this directory, after installing `auto-loop`:

```bash
./bootstrap.sh
auto-loop run --goal-file goal.md
```

`bootstrap.sh` only initializes Git if this folder is not already a repository. It does not run Auto Loop.

## What you edit

| File | Owner |
|------|--------|
| `goal.md` | you — the run goal |
| `auto-loop.yaml` | you — models and limits |
| project files created by the run | you / Auto Loop workers |

`.auto-loop/` is **tool-managed**. You can inspect `plan.md` and `reviews/` there; you do not need to edit that directory to use the sample.
