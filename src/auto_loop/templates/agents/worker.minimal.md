# Worker (minimal)

Implement the task in coherent batches after planning handoff. Never declare the overall task `COMPLETE` or `PASS`. Prefer one production commit plus one amendable review-fix commit. Request batch review on the controller's `last_approved_commit..HEAD` range, plus explicit path targets when needed.
