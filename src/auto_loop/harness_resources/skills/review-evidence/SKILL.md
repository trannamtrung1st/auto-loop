---
name: review-evidence
description: >-
  Perform evidence-driven review of a proposed change. Use for code review, PR review,
  or validating that a diff meets requirements before approval.
---

# Review evidence

## Workflow

1. Read requirements or task description first.
2. Inspect the actual diff and repository state (not only the author's summary).
3. Trace changed interfaces and their callers or tests.
4. Validate tests against claimed behavior; note missing coverage.
5. Classify findings as **blocking** vs **suggestion** with clear rationale.
6. Attach evidence: file paths, line ranges, commands, or logs.
7. Re-check prior findings after revisions; confirm fixes or residual risk.

## Quality bar

- Do not approve solely from the author's narrative.
- Prefer concrete, actionable findings over style nitpicks unless standards require them.
- Acknowledge what was verified vs what was not run.

This skill is generic review guidance and does not embed product-specific completion rules.
