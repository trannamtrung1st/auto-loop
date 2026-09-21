# auto-loop protocol (worker)

- You may never declare the overall task `COMPLETE` or `PASS`.
- After planning handoff you own `plan.md` and may update it when implementation reality requires.
- Request batch review only with a clean product tree. The controller uses `last_approved_commit..HEAD` when Git changed; explicit path targets are allowed for ignored artifacts.
- Never rewrite approved history. Amending an unapproved review-fix commit in the current review cycle is allowed.
