"""Stale persisted active reviews invalidate before reviewer dispatch."""

from __future__ import annotations

from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import ActiveReview, create_lifecycle
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.status_report import build_status_report
from auto_loop.terminal_records import load_completion_record
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    approve_plan,
    batch_worker_payload,
    commit_file,
    execution_reviewer_invocation_count,
    make_repo,
    reviewer_invocation_count,
    run_lifecycle,
    run_opts,
)
from tests.integration.test_review_mutation_recovery import _inject_stale_reviewer_attempt


def _event_types(repo: Path) -> list[str]:
    from tests.repo_utils import frozen_config

    return [event.get("type", "") for event in load_events(repo, frozen_config(repo))]


def _inject_legacy_stale_final(
    repo: Path,
    *,
    candidate: str,
    target: str = "whole-task",
) -> None:
    state = load_lifecycle_state(repo)
    assert state is not None
    plan_path = repo / ".ai" / "auto-loop" / "plan.md"
    from auto_loop.review_targets import sha256_file

    plan_hash = sha256_file(plan_path) if plan_path.is_file() else None
    approved = state.last_approved_commit
    state.active_review = ActiveReview(
        cycle_id="review-stale-001",
        round=1,
        scope="final",
        target=target,
        summary="final handoff",
        session_purpose="reviewer",
        approved_base_commit=approved,
        production_head_commit=approved,
        current_candidate_head=candidate,
        plan_sha256=plan_hash,
    )
    state.next_session = "reviewer"
    state.review_mutation_recovery = None
    state.completed_provider_turn = None
    state.inflight = None
    save_lifecycle_state(repo, state)


def _inject_active_batch_review(repo: Path, *, candidate: str) -> None:
    state = load_lifecycle_state(repo)
    assert state is not None
    from auto_loop.review_targets import sha256_file

    plan_path = repo / ".ai" / "auto-loop" / "plan.md"
    plan_hash = sha256_file(plan_path) if plan_path.is_file() else None
    state.active_review = ActiveReview(
        cycle_id="review-batch-001",
        round=1,
        scope="batch",
        target="W01",
        summary="batch",
        session_purpose="reviewer",
        approved_base_commit=state.last_approved_commit,
        production_head_commit=candidate,
        current_candidate_head=candidate,
        plan_sha256=plan_hash,
    )
    state.next_session = "reviewer"
    state.review_mutation_recovery = None
    state.completed_provider_turn = None
    state.inflight = None
    save_lifecycle_state(repo, state)


def _prepare_batch_approved(repo: Path, provider: ScriptedProvider) -> str:
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit
    return state.last_approved_commit


def test_legacy_stale_final_review_routes_worker_before_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    _inject_legacy_stale_final(repo, candidate=approved, target="P7.6")
    commit_file(repo, "ahead.txt", "y\n", "external change")
    assert head_commit(repo) != approved

    start_reviewers = execution_reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert execution_reviewer_invocation_count(provider) == start_reviewers

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    assert state.active_review is None
    assert state.last_approved_commit == approved
    recovery = state.review_mutation_recovery
    assert recovery is not None
    assert recovery.origin == "stale_evidence"
    assert recovery.scope == "final"
    assert recovery.target == "P7.6"
    assert recovery.expected_head == approved
    assert recovery.observed_head == head_commit(repo)
    assert "active_review_stale" in _event_types(repo)
    assert "active_review_invalidated" in _event_types(repo)

    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review reconciliation:" in report
    assert "reason: active review evidence changed" in report
    assert "scope: final" in report
    assert "target: P7.6" in report
    assert f"expected HEAD: {approved[:7]}" in report
    assert "next session: worker" in report
    assert "active review:" not in report


def test_stale_review_discards_completed_turn_and_inflight(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    _inject_legacy_stale_final(repo, candidate=approved)
    commit_file(repo, "ahead.txt", "y\n", "external change")
    _inject_stale_reviewer_attempt(repo, summary="STALE COMPLETE")

    start = reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert reviewer_invocation_count(provider) == start
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is None
    assert state.inflight is None


def test_worker_receives_stale_reconciliation_guidance(tmp_path: Path):
    repo = make_repo(tmp_path)
    capture = PromptCapturingProvider()
    approved = _prepare_batch_approved(repo, capture)
    _inject_legacy_stale_final(repo, candidate=approved)
    commit_file(repo, "ahead.txt", "y\n", "external change")

    capture.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "waiting",
            "wait": {"reason": "pause", "retry_after_seconds": 3600},
            "work_summary": "reconciling",
            "verification": [],
            "notes": [],
        },
    )
    run_lifecycle(repo, run_opts(1, resuming=True), capture)
    assert capture.worker_prompts
    prompt = capture.worker_prompts[-1]
    assert "persisted review is no longer valid" in prompt
    assert "expected candidate HEAD" in prompt
    assert approved[:7] in prompt


def test_restored_candidate_allows_reviewer_inspection(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    _inject_legacy_stale_final(repo, candidate=approved)
    commit_file(repo, "ahead.txt", "y\n", "mistake")
    git_reset = __import__("subprocess").run(
        ["git", "reset", "--hard", approved],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert git_reset.returncode == 0
    assert head_commit(repo) == approved

    provider.set_reviewer_complete()
    run_lifecycle(repo, run_opts(3, resuming=True), provider)
    assert execution_reviewer_invocation_count(provider) >= 1


def test_stale_final_then_batch_and_complete(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    _inject_legacy_stale_final(repo, candidate=approved)
    new_head = commit_file(repo, "ahead.txt", "y\n", "legitimate work")

    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    provider.set_response("worker", batch_worker_payload(target="W02"))
    provider.set_reviewer_pass("batch", "W02")
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(6), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    assert load_completion_record(repo) is not None
    assert head_commit(repo) == new_head


def test_stale_plan_review_routes_planner(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    from auto_loop.review_targets import plan_path_target, sha256_file
    from auto_loop.config import load_resolved_config_optional

    config = load_resolved_config_optional(repo / ".ai" / "auto-loop")
    assert config is not None
    plan_target = plan_path_target(repo, config.plan_file, git_mode=config.git.mode)
    plan_hash = sha256_file(repo / config.plan_file)
    base = head_commit(repo)
    state = create_lifecycle(base)
    state.phase = "planning"
    state.next_session = "plan_reviewer"
    state.active_review = ActiveReview(
        cycle_id="review-plan-001",
        round=1,
        scope="plan",
        target="plan",
        summary="plan review",
        session_purpose="plan_reviewer",
        plan_sha256=plan_hash,
        targets=[plan_target],
    )
    save_lifecycle_state(repo, state)
    (repo / config.plan_file).write_text("# changed plan\n", encoding="utf-8")

    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    start = reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert reviewer_invocation_count(provider) == start
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "planner"
    assert state.review_mutation_recovery is not None
    assert state.review_mutation_recovery.origin == "stale_evidence"


def test_stale_recovery_clears_on_worker_waiting(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    _inject_legacy_stale_final(repo, candidate=approved)
    commit_file(repo, "ahead.txt", "y\n", "external")
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "waiting",
            "wait": {"reason": "pause", "retry_after_seconds": 3600},
            "work_summary": "reconciling",
            "verification": [],
            "notes": [],
        },
    )
    run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is None


def test_session_ids_unchanged_after_stale_invalidation(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    before = {
        slot: load_lifecycle_state(repo).sessions[slot].session_id
        for slot in load_lifecycle_state(repo).sessions
    }
    _inject_legacy_stale_final(repo, candidate=approved)
    commit_file(repo, "ahead.txt", "y\n", "external")
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    after = {
        slot: load_lifecycle_state(repo).sessions[slot].session_id
        for slot in load_lifecycle_state(repo).sessions
    }
    assert before == after


def test_dirty_product_tree_invalidates_active_review_at_same_head(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    evidence_before = load_lifecycle_state(repo).last_approved_commit
    _inject_active_batch_review(repo, candidate=approved)
    assert head_commit(repo) == approved
    (repo / "feature.txt").write_text("uncommitted product change\n", encoding="utf-8")

    start = execution_reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert execution_reviewer_invocation_count(provider) == start

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    assert state.active_review is None
    assert state.last_approved_commit == evidence_before
    recovery = state.review_mutation_recovery
    assert recovery is not None
    assert recovery.origin == "stale_evidence"
    assert recovery.scope == "batch"


def test_dirty_tree_routes_worker_not_fresh_reviewer_after_failed_complete(
    tmp_path: Path,
):
    from tests.integration.test_untrusted_reviewer_replay import (
        _inject_failed_final_complete,
    )

    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    reviewer_session = load_lifecycle_state(repo).sessions["reviewer"].session_id or "rev-1"
    _inject_failed_final_complete(
        repo,
        candidate=approved,
        reviewer_session=reviewer_session,
        with_repair_inflight=False,
    )
    assert head_commit(repo) == approved
    (repo / "feature.txt").write_text("uncommitted product change\n", encoding="utf-8")

    start = execution_reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert execution_reviewer_invocation_count(provider) == start

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    assert state.reviewer_fresh_inspection_required is False
    assert state.active_review is None
    assert state.completed_provider_turn is None
    assert state.review_mutation_recovery is not None
    assert state.review_mutation_recovery.origin == "stale_evidence"
