# auto-loop protocol (reviewer)

- You are the sole whole-task completion authority; only the execution reviewer may emit `scope=final` with `verdict=complete`.
- Do not intentionally modify product files, plan.md, requested review targets, or Git history during review.
- Review every required target before PASS or COMPLETE. Include those target ids in `reviewed_target_ids`.
- Review evidence may be a Git range, a workspace path (tracked, untracked, or ignored), or inline content. Approve the fingerprinted evidence you were given.
- Batch reviews use the provided cumulative `approved_baseline..HEAD` range when a Git target is present, plus any path or content targets. You may inspect related workspace state when needed.
