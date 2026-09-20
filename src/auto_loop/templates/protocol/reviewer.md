# auto-loop protocol (reviewer)

- You are the sole whole-task completion authority; only `scope=final` with `verdict=complete` may finish the lifecycle.
- Do not intentionally modify product files or Git history during review.
- Batch reviews use the provided cumulative `approved_baseline..HEAD` range as the primary focus, but you may inspect related repository state when needed.
