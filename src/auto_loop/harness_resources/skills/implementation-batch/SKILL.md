---
name: implementation-batch
description: >-
  Turn a plan item into one coherent, reviewable implementation batch. Use when
  implementing a scoped feature, fix, or refactor that should land as a single review unit.
---

# Implementation batch

## Workflow

1. **Intent** — State what this batch will and will not change.
2. **Interfaces** — Inspect affected APIs, configs, and callers before coding.
3. **Implement narrowly** — Match existing style; avoid unrelated cleanup or drive-by refactors.
4. **Tests** — Add or update tests for behavior you change.
5. **Verify** — Run targeted checks for touched areas; expand only when justified.
6. **Self-review** — Read the full diff; confirm scope matches intent.
7. **Commit** — One coherent commit (or the project's stated convention) with a clear message.

## Success evidence

- Diff matches stated intent.
- Tests or checks relevant to the change were run with recorded outcomes.
- No unrelated files or speculative abstractions without justification.

This skill is generic: it does not assume a particular lifecycle controller or state directory layout.
