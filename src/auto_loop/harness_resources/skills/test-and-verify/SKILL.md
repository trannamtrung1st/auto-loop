---
name: test-and-verify
description: >-
  Select and run efficient verification after code changes. Use after implementing a fix,
  before opening a PR, or when asked to prove behavior with tests and checks.
---

# Test and verify

## Workflow (expand only when needed)

```text
targeted tests for changed modules
→ lint/type/static checks for touched area
→ broader affected-suite tests
→ build / integration / e2e when justified by risk or scope
```

## Reporting rules

Label each check explicitly:

| Status | Meaning |
|--------|---------|
| **pass** | Command ran and succeeded |
| **fail** | Command ran and failed |
| **not run** | Deliberately skipped (say why) |
| **blocked** | Could not run (missing tool, env, credentials) |

Never report **not run** or **blocked** as **pass**.

## Success evidence

- Commands with exit status or summarized output.
- Scope tied to files or behaviors changed.
- Failures reproduced before claiming a fix.

Prefer repository-documented scripts over ad-hoc invocations when both exist.
