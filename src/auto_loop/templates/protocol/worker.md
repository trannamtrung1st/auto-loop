# auto-loop protocol (worker)

- You may never declare the overall task `COMPLETE` or `PASS`.
- Before initial plan approval, do not create product implementation commits.
- After plan approval, request batch review only with a clean product tree and committed work on the controller's exact `last_approved_commit..HEAD` range.
- Do not amend, rebase, or rewrite commits already submitted for review.
