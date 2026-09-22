# Developing auto-loop

This file and `.agents/skills/` are a **contributor harness** for humans and coding agents working **on the auto-loop repository**. They are not part of the auto-loop application runtime, are not copied by `auto-loop init`, and are not packaged in the wheel. Target repositories may have their own `AGENTS.md` and skills; auto-loop respects those at runtime but does not install this harness into them.

`auto-loop` is a **mechanical controller** for a planner / worker / reviewer lifecycle. Agents own semantic reasoning. The controller owns protocol, Git, session identity, protection, and recovery. Do not leak task semantics into controller scheduling.

## Durable facts

- Reviewer remains the sole completion authority. Planning `PASS` is not task completion.
- Persistent session identity is a protocol invariant. Never silently rotate sessions.
- When an approved Git head exists and history protection is enabled, that baseline's ancestry is a hard safety invariant. Never rewrite approved history. Git is review evidence, not the only way a review can pass.
- Planning and execution reviewer sessions are intentionally isolated (`plan_reviewer` vs `reviewer`).
- `plan.md` is planner-owned before initial PASS and worker-owned after handoff. `task.md` stays authoritative.
- Prefer changing existing modules over introducing parallel orchestration abstractions.
- Preserve stable exit-code meanings unless an explicit versioned migration requires change.
- New behavior needs unit tests and fake-provider integration coverage.
- Run focused tests first, then the full offline pytest suite. Ordinary CI (GitHub Actions on pull requests and `main`) must not require live Cursor.

## Contributor skills

| When | Skill |
|------|--------|
| Implementing or fixing auto-loop | `auto-loop-develop` |
| Reviewing a change before merge/release | `auto-loop-review` |

Skills are workflow-oriented guides (discovery, invariants, checklists inside one file). They are not runtime roles the controller dispatches.

## Two planes

- **Development (this repo):** `AGENTS.md` + `.agents/skills/**` — for contributors maintaining auto-loop.
- **Runtime (target task):** CLI, user-owned run YAML + task document, configured artifact root — for planner/worker/reviewer agents on a user task.
