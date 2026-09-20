---
name: repo-discovery
description: >-
  Quickly understand an unfamiliar repository before planning or editing. Use when
  onboarding to a new codebase, starting a feature, or unsure where code and tests live.
---

# Repo discovery

## Workflow

1. Read root instructions (`README`, `AGENTS.md`, `CONTRIBUTING`) and package/build manifests (`pyproject.toml`, `package.json`, `go.mod`, etc.).
2. Map architecture boundaries (apps vs libraries, layers, major packages).
3. Find canonical test, lint, type-check, and build commands from docs or scripts.
4. Locate examples similar to the task (existing features, tests, CLI commands).
5. Skim recent relevant history only when it clarifies intent or constraints.
6. Summarize constraints, risks, and a suggested approach **before** large edits.

## Success output

A short, evidence-based brief:

- how to build and test;
- where to change code;
- patterns to follow;
- open questions or blockers.

Do not invent commands; cite files or scripts you inspected.
