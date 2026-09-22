# auto-loop protocol (worker)

- You may never declare the overall task `COMPLETE` or `PASS`.
- After planning handoff you own `plan.md` and may update it when implementation reality requires.
- Request review when the intended review evidence is stable. Include explicit path or content targets for uncommitted or non-Git work.
- When a meaningful Git commit range exists, the controller includes `last_approved_commit..HEAD` as review evidence. Do not request review with an empty range and no other targets.
- In strict Git mode (`git.mode: required`), commit product changes and request batch review only with a clean product tree.
- Never rewrite approved history. Amending an unapproved review-fix commit in the current review cycle is allowed.
