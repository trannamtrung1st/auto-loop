---
name: auto-loop-test-and-verify
description: >-
  Choose and run the auto-loop verification matrix after implementation. Use when
  deciding which tests to run, reporting results, or proving behavior without
  claiming unrun live Cursor checks.
---

# auto-loop test and verify

Recommended flow:

```text
focused unit tests
→ focused integration scenario(s)
→ complete offline pytest suite
→ packaging/CLI checks if affected
→ optional live Cursor smoke if provider behavior changed
```

Prefer scenario-style tests under `tests/integration/` over relying only on isolated unit tests. Integration tests use the in-process fake/scripted provider and real temporary Git repositories.

## Reporting rules

| Status | Meaning |
|--------|---------|
| **pass** | Command ran and succeeded |
| **fail** | Command ran and failed |
| **not run** | Deliberately skipped (say why) |
| **not applicable** | Check does not apply to this change |
| **blocked** | Could not run (missing tool, env, credentials) |

Never report **not run**, **not applicable**, or **blocked** as **pass**. Never claim live-provider verification when only the fake provider ran.

Ordinary CI: `python -m pytest -q` (no live Cursor). Live smoke: `AUTO_LOOP_LIVE_CURSOR=1 pytest tests/integration/test_live_cursor_smoke.py -m live_cursor`.
