---
name: ui-validation
description: >-
  Validate frontend UI behavior beyond static code review. Use for web UI changes,
  forms, navigation, responsive layout, or when visual/interaction bugs are suspected.
---

# UI validation

## Workflow

1. Run the app locally or use the project's documented preview environment.
2. Exercise critical user flows (happy path and obvious error paths).
3. Check responsive breakpoints when layout changed.
4. Open browser devtools: fix or report console errors and failed network calls.
5. Apply basic accessibility checks (labels, focus order, keyboard use) when feasible.
6. Capture screenshots or visual diffs when the repo provides comparison tooling.

## Success evidence

- Steps performed in the real UI (not only source inspection).
- Browsers/viewports exercised.
- Console/runtime errors noted or cleared.
- Screenshots or recordings when useful for reviewers.

Adapt browser and automation tools to what the repository already uses; do not mandate a specific vendor stack.
