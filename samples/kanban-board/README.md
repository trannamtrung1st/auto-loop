# Kanban board sample

A clean Auto Loop project: one run manifest and one proposal, with no checked-in generated state.

## Quick start

From this directory, after installing `auto-loop`:

```bash
auto-loop doctor .ai/run.yaml
auto-loop run .ai/run.yaml
```

If this folder is not already a Git repository, initialize it first (`git init` and an initial commit). `bootstrap.sh` can do that for local convenience; it is not part of the Auto Loop user contract.

## What you edit

| File | Owner |
|------|--------|
| `.ai/proposal.md` | you — the task/goal |
| `.ai/run.yaml` | you — realistic example (models, run limits, context, logging) |
| project files created by the run | you / Auto Loop workers |

Generated plan, reviews, and runtime state live under `.ai/auto-loop/` (tool-managed). You do not need to edit that directory.

For every supported public setting with defaults and comments, generate a reference manifest:

```bash
auto-loop init .ai/run.yaml --full
```

That command is optional; use it when you want the complete configuration surface in one YAML file.
