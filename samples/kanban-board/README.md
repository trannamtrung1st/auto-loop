# Kanban board sample

These files are the **initial user input** for Auto Loop before any run. The tree is intentionally minimal: a run manifest, a task entry, and one ordinary requirements document. Nothing here is generated lifecycle state.

## Quick start

From this directory, after installing `auto-loop`:

```bash
auto-loop doctor .ai/run.yaml
auto-loop run .ai/run.yaml
```

No Git repository is required. There is no bootstrap script. Run the commands above directly against this folder.

`git.mode` is `off` in `.ai/run.yaml` on purpose. This sample lives inside the Auto Loop repository when you clone it; optional Git discovery could otherwise pick up the parent repo and treat its history as review evidence. This example is a plain-directory demonstration.

## What you edit

| File | Role |
|------|------|
| `.ai/task.md` | Auto Loop entry for the run; references `proposal.md` |
| `proposal.md` | Authoritative product requirements (listed in `task.resources`) |
| `.ai/run.yaml` | Run manifest (models, limits, context, logging) |

## What appears only after a run

Auto Loop creates tool-managed state under `.ai/auto-loop/` when you run (frozen task snapshot, plan, reviews, runtime). That directory is **not** part of the checked-in sample. If you run this sample inside a clone of Auto Loop, the repository root `.gitignore` ignores `samples/**/.ai/auto-loop/` so local runs do not pollute Git status.

For every supported public setting with defaults and comments, you can generate a reference manifest:

```bash
auto-loop init .ai/run.yaml --full
```

That command is optional and does not modify this sample unless you pass `--force`.
