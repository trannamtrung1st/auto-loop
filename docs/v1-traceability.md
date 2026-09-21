# v1 requirement traceability (proposal sections 1–48)

This matrix maps **acceptance criteria (§47)**, **architectural decisions (§48)**, **workflow scenarios A–T (§44)**, and **enhancement scenarios U–AK (§32)** to implementation paths and verification. **Section 49** (future extensions) is explicitly **out of scope** — no implementation claims below.

## Scenarios A–T and U–AK → tests

| Scenario | Intent | Primary test |
|----------|--------|----------------|
| A | Happy path through plan, batch, final `COMPLETE` | `tests/integration/test_scenarios_a_j.py::test_scenario_A_happy_path` |
| B | Plan revision without product commits before PASS | `test_scenario_B_plan_revision_no_product_commits_before_pass` |
| C | Cumulative batch range A..C (not B..C) | `test_scenario_C_batch_revision_reviews_cumulative_A_to_C_not_B_to_C` |
| D | Multiple revision commits; PASS on A..D | `test_scenario_D_multiple_revision_commits_accept_A_to_D` |
| E | Wrong worker base normalized to approved..HEAD | `test_scenario_E_wrong_base_normalized_to_approved_through_head` |
| F | Dirty batch suppresses reviewer until protocol exhaustion | `test_scenario_F_dirty_batch_suppresses_reviewer_until_exhaustion` |
| G | History rewrite → `GIT_PROTOCOL_ERROR` | `test_scenario_G_history_rewrite_stops_with_git_protocol_error` |
| H | Final blocked when HEAD ahead of approved | `test_scenario_H_final_rejected_when_head_ahead_of_approved` |
| I | Final revise routes through batch before `COMPLETE` | `test_scenario_I_final_revise_routes_through_batch_before_complete` |
| J | Reviewer finding outside diff accepted | `test_scenario_J_reviewer_finding_outside_diff_is_accepted` |
| K | Persistent planner session id | `tests/integration/test_scenarios_k_t.py::test_scenario_K_persistent_planner_session` |
| L | Persistent plan-reviewer session id | `test_scenario_L_persistent_plan_reviewer_session` |
| M | Session mismatch → `SESSION_ERROR` | `test_scenario_M_session_mismatch_returns_session_error` |
| N | Interrupted worker reconciles existing commit | `test_scenario_N_controller_interruption_reconciles_existing_commit` |
| O | Provider crash retries same session | `test_scenario_O_provider_crash_retries_same_session` |
| P | Protocol repair same session | `test_scenario_P_protocol_failure_repairs_same_session` |
| Q | Reviewer product mutation → `REVIEW_MUTATION_ERROR` | `test_scenario_Q_reviewer_product_mutation_invalidates_verdict` |
| R | Protected task mutation → `PROTECTION_VIOLATION` (no revert) | `test_scenario_R_protected_task_mutation_stops_without_revert` |
| S | Blocked worker; reviewer `REVISE` vs `BLOCKED` | `test_scenario_S_blocked_worker_reviewer_revise_resumes_worker`, `test_scenario_S_blocked_worker_reviewer_blocked_writes_record` |
| T | Limits → `LIMIT_REACHED`, never `COMPLETE` | `test_scenario_T_limit_reached_never_complete` |
| U | Dedicated planner; execution sessions absent before plan PASS | `tests/integration/test_scenarios_u_ak.py::test_scenario_U_planning_uses_dedicated_planner` |
| V | Plan revision preserves planner and plan-reviewer sessions | `test_scenario_V_plan_revision_preserves_planning_sessions` |
| W | Execution sessions are fresh and all four ids are distinct | `test_scenario_W_execution_sessions_are_fresh` |
| X | Independent planner/worker/reviewer model selection | `test_scenario_X_three_model_selections` |
| Y | CLI role override beats `--model` and config | `test_scenario_Y_cli_role_override_precedence` |
| Z | Worker may update approved plan without protection violation | `test_scenario_Z_worker_updates_approved_plan` |
| AA | Execution-phase plan review uses execution reviewer only | `test_scenario_AA_worker_requests_execution_phase_plan_review` |
| AB | Preferred single amendable review-fix commit | `test_scenario_AB_preferred_single_revision_commit` |
| AC | Multiple revision commits remain legal | `test_scenario_AC_multiple_revision_commits_still_accepted` |
| AD | No-op revision without empty commit | `test_scenario_AD_noop_revision` |
| AE | Gitignored path-only review does not advance baseline | `test_scenario_AE_gitignored_path_only_review` |
| AF | Mixed Git + ignored artifact review requires both target ids | `test_scenario_AF_mixed_git_and_ignored_artifact_review` |
| AG | Dirty tracked source cannot bypass commit via path target | `test_scenario_AG_dirty_tracked_source_cannot_bypass_commit` |
| AH | Reviewer mutating plan.md → `REVIEW_MUTATION_ERROR` | `test_scenario_AH_reviewer_mutates_plan` |
| AI | Reviewer mutating requested ignored artifact → `REVIEW_MUTATION_ERROR` | `test_scenario_AI_reviewer_mutates_requested_ignored_artifact` |
| AJ | Recovery during planning resumes the planning session | `test_scenario_AJ_recovery_during_planning` |
| AK | Recovery after handoff resumes execution sessions only | `test_scenario_AK_recovery_after_planning_handoff` |

Supporting integration coverage: `test_plan_flow.py`, `test_lifecycle_flows.py`, `test_final_completion.py`, `test_recovery.py`, `test_stop_and_limits.py`, `test_observability.py`, `test_scenarios_u_ak.py`.

## §47 acceptance → implementation / verification

| Area | Requirement (summary) | Implementation | Verification |
|------|----------------------|----------------|--------------|
| Setup / CLI | `init`, `doctor`, `run`, `resume`, `migrate`; user-owned `auto-loop.yaml` + goal; frozen config on resume | `init_cmd.py`, `run_inputs.py`, `config.py`, `cli.py`, `loop.py` | `test_init.py`, `test_cli.py`, `test_ux_contract.py`, `test_config.py`, `test_doctor.py`, integration flows |
| Persistent sessions | Four slots: planner, plan_reviewer, worker, reviewer; resume; mismatch | `loop.py`, `providers/cursor.py`, `lifecycle.py` | Scenarios K–M, `test_fake_cursor.py`, `test_cursor_stream.py` |
| Planning | Dedicated planner + plan-reviewer; retire after PASS | `loop.py`, `prompts.py` | Scenarios A–B, U–V, `test_plan_flow.py`, `test_retired_planning_slots_reject_resume_after_handoff` |
| Batch | Clean tree; `last_approved..HEAD`; no rewrite | `git.py`, `loop.py` | Scenarios C–G, `test_git.py` |
| Revision | Baseline only on PASS; cumulative range; amendable review-fix | `git.normalize_batch_range`, `loop.py` | Scenarios C–D, AB, `test_lifecycle_flows.py`, `test_git.py` |
| Reviewer scope | Primary diff + widened inspection; no product edits | `prompts.py`, `protection.py` | Scenarios J, Q; `test_protection.py` |
| Final | HEAD == approved; `COMPLETE` only from reviewer | `loop.py`, `terminal_records.py` | Scenarios H–I, `test_final_completion.py` |
| Instructions / resources | Protocol + extend/replace; context manifest; root development harness | `instructions.py`, `context_manifest.py`, `AGENTS.md`, `.agents/skills/` | `test_instructions.py`, `test_context_manifest.py`, `test_development_harness.py` |
| Resilience | Retries, inflight, locks, repair | `supervision.py`, `subprocess_cursor.py`, `locking.py`, `atomic_io.py`, `loop.py` | Scenarios N–P, `test_recovery.py`, `test_locking.py`, `test_supervision.py` |
| Monitoring | Events, turn logs, status, logs, stop | `events.py`, `turn_logs.py`, `status_report.py`, `logs_view.py`, `stop_control.py` | `test_observability.py`, `test_events.py`, `test_status_report.py` |
| Limits | Turns, runtime, no-progress; distinct exit codes | `limits.py`, `exits.py`, `loop.py` | Scenario T, `test_limits.py`, `test_stop_and_limits.py` |
| Tests | Unit + fake integration + documented live smoke | `tests/`, `live_smoke.py` | `pytest -q`; `docs/live-cursor-smoke.md` |
| Operator docs | Install, commands, exit codes, troubleshooting | `README.md` | `test_packaging.py`, manual review |

## §48 architectural decisions (1–28)

| # | Decision | Code / behavior anchor |
|---|----------|------------------------|
| 1–3 | Three roles / four session slots; no orchestrator; one actor at a time | `lifecycle.py` `next_session`, `loop.py` slot dispatch |
| 4–5 | Persistent sessions; explicit `--resume` | `loop._invoke_slot`, `build_cursor_command` |
| 6–8 | Planner owns initial plan; worker owns plan after handoff; task authoritative; plan before implementation | `loop._planner_turn`, `loop._worker_turn` plan gates, scenarios B, U, Z |
| 9–12 | Worker batches; commits before review; controller range; cumulative revise | `git.normalize_batch_range`, scenarios C–D |
| 13–14 | Reviewer widened scope; PASS advances baseline | `prompts.build_reviewer_prompt`, scenario J |
| 15–18 | Final HEAD check; holistic final; batch before re-final; reviewer `COMPLETE` | `loop._assert_final_complete_valid`, scenarios H–I |
| 19–23 | `.auto-loop` control; no plan parsing; no session rotation; no auto controller commits; simple state machine | `paths.py`, `lifecycle.py`, `loop.py` |
| 24–27 | Tool protocol; instruction composition; context paths; repository-root AGENTS/skills | `instructions.py`, `templates/protocol/*`, `AGENTS.md`, `.agents/skills/` |
| 28 | No extra orchestration without invariant need | — (design stance) |

## §49 exclusion

Not implemented: specialist reviewers, orchestrator agent, parallel workers, PR management, dashboards, automatic session rotation, additional providers as shipped features. README and this document state they are **non-goals**.

## Production commit index (v1 implementation line)

Representative commits on `main` (newest first): see `git rev-parse HEAD` and `git log --oneline -5`. Recent milestones include streaming `logs --follow` (`bf4885a`), live smoke verification (`013dc65`), §45/`--trust` (`c26bbf3`), traceability docs (`b93d1f1`), live harness (`8b321e3`), and scenario coverage through `7722918` / `3a90ee1`. Full history: `git log --oneline`.
