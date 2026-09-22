# Samples

Runnable Auto Loop example projects. Copy a sample out of this repository so Auto Loop does not treat the auto-loop source tree as the product.

Each sample follows the user-facing contract:

- `.ai/run.yaml` — explicit run manifest
- `.ai/task.md` — Auto Loop entry for the run
- `proposal.md` — ordinary requirements, declared in `task.resources`
- no root `auto-loop.yaml` / `context.yaml`
- no checked-in generated artifact state
