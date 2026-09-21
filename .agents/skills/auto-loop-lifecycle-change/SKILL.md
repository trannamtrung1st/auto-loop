---
name: auto-loop-lifecycle-change
description: >-
  Change planner/worker/reviewer flow, phase, session slots, recovery, blockers,
  stop, or completion. Use before editing lifecycle.py, loop.py, or session identity.
---

# auto-loop lifecycle change

Write the intended **mechanical** state transition first. Then implement.

## Required checks

- Identify durable state additions/removals (`LifecycleState`, runtime schema).
- Check crash/inflight recovery for the affected session slot.
- Verify session identity: persist ids, detect mismatch, never silently rotate.
- Verify terminal states (`completed`, `blocked`, `stopped`, `limit_reached`, `error`).
- Update status/events where the operator-visible phase or slot changed.
- Add integration scenarios for the happy path and an interruption/revision path.

## Anti-patterns

Do not add semantic orchestration, controller-owned plan-item scheduling, or a DAG of task meaning. The controller does not choose what to build.

Planning sessions (`planner`, `plan_reviewer`) retire after initial plan PASS. Later plan updates go to the execution reviewer in a fresh execution session — do not resume retired planning sessions.
