---
name: auto-loop-repo-discovery
description: >-
  Map auto-loop architecture before changing it. Use when starting work on this
  repository, onboarding, or when architecture context is stale.
---

# auto-loop repo discovery

Inspect in this order:

1. `AGENTS.md`
2. `README.md`
3. Relevant proposal/design docs under `local/proposals/` and `docs/`
4. `src/auto_loop/lifecycle.py`
5. `src/auto_loop/loop.py`
6. `src/auto_loop/models.py` and `src/auto_loop/protocol.py`
7. Relevant provider modules under `src/auto_loop/providers/`
8. Matching unit and integration tests under `tests/`
9. Recent relevant Git history only when it clarifies intent

Do not invent commands; cite files you inspected.

## Success output

A concise implementation map:

```text
affected invariants
affected modules
affected tests
migration/compatibility risks
```

Include how to build/test (`python -m pytest`, `python -m pip install -e ".[dev]"`), where to change code, patterns to follow, and open questions.
