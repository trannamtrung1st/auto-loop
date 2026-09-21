---
name: auto-loop-protocol-state
description: >-
  Change result schemas, review requests/targets, Git boundaries, plan hashes,
  protected files, fingerprints, or persistence schema. Use for models.py,
  protocol.py, git.py, protection.py, and runtime state migrations.
---

# auto-loop protocol and state

Before coding, distinguish:

```text
agent advisory behavior
vs
controller-enforced invariant
```

Advisory text lives in templates/prompts. Invariants belong in the controller and must fail closed.

## Required work

- Protocol model changes (`models.py`) and parser validation (`protocol.py`)
- Runtime schema / migration decision (`lifecycle.py`, `runtime.py`)
- Malformed and forbidden-output tests
- Git and reviewer-mutation invariant tests
- Review artifact compatibility
- Backwards-compatibility notes for config vs runtime state

## Hard invariants (do not weaken)

- Approved baseline remains an ancestor of HEAD
- Final review requires `HEAD == last_approved_commit`
- Reviewer cannot mutate product state or requested review targets
- Path targets cannot bypass committing dirty tracked/untracked product source
- `PASS` advances `last_approved_commit` only for a non-empty Git candidate
