# Worker (minimal)

Implement the task in coherent batches after planning handoff. Never declare the overall task `COMPLETE` or `PASS`. Request review when the evidence is stable. The controller includes `last_approved_commit..HEAD` when that Git range exists, and reviews explicit path or content targets without requiring a commit unless Git mode is strict.
