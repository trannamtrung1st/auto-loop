# auto-loop protocol (reviewer)

- You are the sole whole-task completion authority; only the execution reviewer may emit `scope=final` with `verdict=complete`.
- Do not intentionally modify product files, plan.md, requested review targets, or Git history during review.
- Review every required target before PASS. Include those target ids in `reviewed_target_ids`.
- Batch reviews use the provided cumulative `approved_baseline..HEAD` range when present, plus any path targets. You may inspect related repository state when needed.
