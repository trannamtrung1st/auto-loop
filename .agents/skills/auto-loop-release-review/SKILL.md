---
name: auto-loop-release-review
description: >-
  Evidence-driven review before treating an auto-loop enhancement as
  implementation-ready. Use before release, merging a large lifecycle change, or
  claiming the proposal is complete.
---

# auto-loop release review

This is the development-repository equivalent of an evidence-driven final review. It is not the runtime reviewer protocol.

Review:

- Proposal/implementation consistency
- Config defaults and migration
- State-schema migration
- CLI and docs consistency
- Exit codes
- Generated templates (`src/auto_loop/templates/`)
- Fake-provider scenarios
- Package contents — no `auto_loop.harness_resources`, no resources-install command
- README / traceability updates
- Full offline suite
- Optional live smoke status (honest: pass / fail / not run / blocked)

Do not approve solely from the author's narrative. Attach commands, paths, and residual risks. Confirm the development harness remains only at repository-root `AGENTS.md` and `.agents/skills/`.
