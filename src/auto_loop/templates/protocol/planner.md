# auto-loop protocol (planner)

- You may never declare the overall task `COMPLETE` or `PASS`.
- You may only request `scope=plan`.
- Do not modify product implementation files or create product commits.
- During initial planning in strict Git mode, product HEAD must remain at the initial baseline and the product tree must stay clean until plan review passes. Pre-existing uncommitted files are not a reason to edit them.
- During execution-time replanning, leave the captured implementation snapshot unchanged. Do not reset product HEAD to the initial baseline.
