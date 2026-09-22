# Kanban board sample

A clean Auto Loop project: one run manifest, one task entry, and one ordinary proposal. No checked-in generated state.

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
| `.ai/task.md` | you — Auto Loop entry; points at `proposal.md` |
| `proposal.md` | you — ordinary product requirements, listed in `task.resources` |
| `.ai/run.yaml` | you — realistic example (models, run limits, context, logging) |
| `.gitignore` | you — ignores tool-managed `.ai/auto-loop/` while keeping the run YAML, task entry, and proposal tracked |
| project files created by the run | you / Auto Loop workers |

`.ai/task.md` can be prepared before `auto-loop run`. Auto Loop snapshots it to `.ai/auto-loop/task.md` and snapshots `proposal.md` to `.ai/auto-loop/task-resources/proposal.md` when a lifecycle starts. An active lifecycle keeps those frozen copies even if you later edit the originals.

Generated plan, reviews, and runtime state also live under `.ai/auto-loop/` (tool-managed). You do not need to edit that directory.

For every supported public setting with defaults and comments, generate a reference manifest:

```bash
auto-loop init .ai/run.yaml --full
```

That command is optional; use it when you want the complete configuration surface in one YAML file.
