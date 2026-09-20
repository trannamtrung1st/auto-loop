# Agent engineering habits

Use this file as repository-agnostic guidance for AI-assisted development. It does not replace project-specific instructions in README, CONTRIBUTING, or tool-specific config.

## Before you edit

- Read root instructions (`README`, `AGENTS.md`, `CONTRIBUTING`, package manifests) before changing code.
- Prefer existing patterns, modules, and naming over parallel abstractions.
- Preserve unrelated user changes; keep diffs focused on the requested work.

## Implementation discipline

- Make minimal coherent changes that solve the stated problem.
- Update or add tests when behavior changes; do not hide failing tests.
- Run focused checks on touched areas before broad suites when time matters.
- Use repository scripts (`make`, `npm test`, `pytest`, etc.) before inventing one-off commands.
- Do not claim success without evidence (command output, test results, or observable behavior).

## Verification and review

- Distinguish **not run**, **pass**, **fail**, and **blocked**; never treat a skipped check as a pass.
- Attach paths, commands, or logs when reporting findings or blockers.
- Avoid destructive Git operations (force push, hard reset, history rewrite) unless explicitly required.

## Artifacts

- Keep generated build outputs out of source control unless the repository expects them.
- Follow the project's formatter, linter, and type-check conventions when present.
