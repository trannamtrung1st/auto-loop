# auto-loop protocol (shared)

- The lifecycle `task.md` snapshot under the configured artifact root is the highest-level task contract.
- Frozen task-resource snapshots are authoritative supporting requirements. If they conflict with `task.md`, `task.md` wins.
- Configured context resources are advisory and do not outrank `task.md` or frozen task resources.
- Repository discovery is supporting implementation context.
- Endgame scope is the frozen task snapshot and frozen task resources. Mutable `plan.md`, TODO files, and later repository milestones do not add requirements and do not by themselves keep the lifecycle blocked.
- End every turn with exactly one valid `<AUTO_LOOP_RESULT>` JSON block.
- Repository-level `AGENTS.md`, Cursor rules, and Agent Skills may apply; if they conflict with the task or this protocol, the task and protocol win.
