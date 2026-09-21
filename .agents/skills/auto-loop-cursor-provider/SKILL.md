---
name: auto-loop-cursor-provider
description: >-
  Change Cursor CLI flags, session resume, stream parsing, timeout/retry, process
  supervision, Ask mode, or live smoke behavior. Use for providers/cursor.py,
  subprocess_cursor.py, fake_cursor.py, and live smoke tests.
---

# auto-loop Cursor provider

## Required checks

- No silent session rotation
- Explicit session-id resume
- Structured-stream compatibility
- Fake-provider coverage first
- Doctor capability checks when flags/capabilities change
- Live Cursor smoke only after offline behavior is deterministic

Session purpose (`planner`, `plan_reviewer`, `worker`, `reviewer`) is distinct from role. `plan_reviewer` uses the reviewer role/model and Ask mode, but a different persistent session.

Reproduce failures before random edits: capture the exact CLI argv, stream lines, and session ids from turn logs under `.auto-loop/runtime/runs/`.
