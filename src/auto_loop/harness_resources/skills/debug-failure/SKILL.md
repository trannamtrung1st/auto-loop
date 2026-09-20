---
name: debug-failure
description: >-
  Investigate a failing test, build, or runtime behavior without random edits. Use when
  CI fails, a test regresses, or reproduction steps are available.
---

# Debug failure

## Workflow

1. **Reproduce** — Run the failing command locally with the same flags/env when possible.
2. **Capture** — Save exact error text, stack trace, or log excerpt.
3. **Minimize** — Narrow to the smallest failing case (single test, minimal input).
4. **Hypothesize** — Form theories from evidence, not guesses.
5. **Root cause** — Trace ownership (recent diff, dependency, config, data).
6. **Fix smallest root cause** — Avoid masking symptoms.
7. **Rerun** — Confirm the originally failing check passes.
8. **Regression** — Run related tests for the touched area.

## Anti-patterns

- Increasing timeouts or deleting assertions to make checks green without fixing behavior.
- Broad refactors unrelated to the failure.
- Claiming fixed without rerunning the failing command.

## Success evidence

- Reproduction command and before/after outcome.
- Explanation linking cause to fix.
- Related checks run after the fix.
