---
name: auto-loop-review
description: >-
  Independently review a change to the auto-loop repository for lifecycle
  correctness, protocol invariants, migrations, session identity, Git safety,
  recovery, tests, docs, and packaging. Use before merge or release.
---

# auto-loop review

Review like an execution reviewer, but for **contributor changes** to auto-loop — not for runtime task completion.

## Invariants (fail if violated)

- Execution reviewer is sole **task** completion authority; planning PASS ≠ done.
- Four session slots; planning sessions **retire** after initial plan PASS and must not resume.
- Session IDs persist; no silent rotation; retries must resume the same Cursor session once an id is observed.
- Reviewer results must match the controller’s **active review** (scope, target, Git range when claimed).
- `last_approved_commit` advances only on batch PASS with a non-empty Git candidate (path-only PASS does not move baseline).
- Approved Git history is never rewritten.
- Root `.agents/skills/` is contributor-only — not in the wheel, not in `init`, not a runtime dependency.

## Change-type checklist

- **Lifecycle / loop:** slot dispatch, inflight recovery, stop, limits, reviewer binding, pending revision rounds.
- **Protocol / state:** schema v2 fields, migrations (including in-progress v1 reviews), completion/blocked records.
- **Config:** planner defaults, protected-file merge, model precedence.
- **Templates / prompts:** role boundaries, no harness leakage into runtime templates.
- **Tests:** scenario coverage, negative paths, `test_development_harness.py` contract (not fixed skill names).
- **Docs:** README, traceability, smoke runbook counts.

## Evidence required

Report with:

```text
focused pytest result (commands + pass/fail/skip)
additional targeted checks relevant to the change
full offline suite: result, or "not run (not explicitly requested)"
residual risks
```

Run focused tests by default. Do not run the full offline suite (`python -m pytest -q`) unless the user explicitly requests it. Packaging/harness tests are required only when those areas are affected. Do not approve from narrative alone. Optional live smoke: state pass / fail / not run / blocked honestly.
