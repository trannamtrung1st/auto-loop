# Developing auto-loop

`auto-loop` is a **mechanical controller** for a planner / worker / reviewer lifecycle. Agents own semantic reasoning. The controller owns protocol, Git, session identity, protection, and recovery. Do not leak task semantics into controller scheduling.

## Durable facts

- Reviewer remains the sole completion authority. Planning `PASS` is not task completion.
- Persistent session identity is a protocol invariant. Never silently rotate sessions.
- Git approved-baseline ancestry is a hard safety invariant. Never rewrite approved history.
- Planning and execution reviewer sessions are intentionally isolated (`plan_reviewer` vs `reviewer`).
- `plan.md` is planner-owned before initial PASS and worker-owned after handoff. `task.md` stays authoritative.
- Prefer changing existing modules over introducing parallel orchestration abstractions.
- Preserve stable exit-code meanings unless an explicit versioned migration requires change.
- New behavior needs unit tests and fake-provider integration coverage.
- Run focused tests first, then the full offline pytest suite. Ordinary CI must not require live Cursor.

## Skills

| Change | Skill |
|--------|--------|
| Starting work / stale architecture context | `auto-loop-repo-discovery` |
| Phase, session-slot, recovery, stop, completion | `auto-loop-lifecycle-change` |
| Result schemas, Git/review targets, persistence | `auto-loop-protocol-state` |
| Cursor CLI, resume, streams, supervision | `auto-loop-cursor-provider` |
| Choosing and reporting verification | `auto-loop-test-and-verify` |
| Implementation-ready / release check | `auto-loop-release-review` |

Root `.agents/skills/` is the only development harness. Do not reintroduce a packaged installer or copy these skills into target `.auto-loop/` workspaces.

## Two planes

- **Development:** this file and `.agents/skills/**` — for contributors working on this repository.
- **Runtime:** the CLI, `.auto-loop/context.yaml`, and generated role templates — for planner/worker/reviewer agents on a target task.
