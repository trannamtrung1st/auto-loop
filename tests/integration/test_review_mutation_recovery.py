"""Resume reconciles reviewer product mutation instead of replaying the old verdict."""

from __future__ import annotations

import os
from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import CompletedProviderTurn, InflightMarker, LifecycleStatus, utc_now
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.status_report import build_status_report
from auto_loop.terminal_records import load_completion_record
from tests.integration.scenario_harness import (
    approve_plan,
    batch_worker_payload,
    batch_worker_payload_with_path,
    commit_file,
    git,
    latest_review_text,
    make_repo,
    run_lifecycle,
    run_opts,
)
from tests.repo_utils import frozen_config
from tests.unit.test_waiting import FakeClock, _suspend_config


class _MutatingReviewer(ScriptedProvider):
    """Mutate one reviewer or plan-reviewer turn, then behave as a normal script."""

    def __init__(self, repo: Path, rel: str, *, commit: bool) -> None:
        super().__init__()
        self.repo = repo
        self.rel = rel
        self.commit = commit
        self.armed = False
        self.mutated = False

    def arm(self) -> None:
        self.armed = True
        self.mutated = False

    def invoke(self, argv: list[str]):
        role = os.environ.get("AUTO_LOOP_FAKE_ROLE")
        if self.armed and not self.mutated and role in ("reviewer", "plan_reviewer"):
            self.mutated = True
            path = self.repo / self.rel
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            path.write_text(existing + "\nreviewer-mutation\n", encoding="utf-8")
            if self.commit:
                git(self.repo, "add", self.rel)
                git(self.repo, "commit", "-m", "reviewer mutation")
        return super().invoke(argv)


def _event_types(repo: Path) -> list[str]:
    return [event.get("type", "") for event in load_events(repo, frozen_config(repo))]


def _inject_stale_reviewer_attempt(repo: Path, *, summary: str) -> None:
    state = load_lifecycle_state(repo)
    assert state is not None
    session_id = state.sessions["reviewer"].session_id or state.sessions["plan_reviewer"].session_id
    assert session_id
    slot = "plan_reviewer" if state.phase == "planning" else "reviewer"
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot=slot,
        role="reviewer",
        turn=state.turn,
        session_id=session_id,
        result_kind="reviewer",
        result={
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "complete" if slot == "reviewer" else "pass",
            "scope": "final" if slot == "reviewer" else "plan",
            "target": "whole-task" if slot == "reviewer" else "plan",
            "whole_task_reviewed": slot == "reviewer",
            "summary": summary,
            "findings": [],
            "verification": [],
        },
    )
    state.inflight = InflightMarker(
        session_slot=slot,
        role="reviewer",
        turn=state.turn,
        session_id=session_id,
        started_at=utc_now(),
        repair_reason="Re-emit the result only",
        output_only_protocol_repair=True,
    )
    save_lifecycle_state(repo, state)


def _session_ids(repo: Path) -> dict[str, str | None]:
    state = load_lifecycle_state(repo)
    assert state is not None
    return {slot: state.sessions[slot].session_id for slot in state.sessions}


def _final_mutation_with_changed_head(
    tmp_path: Path,
) -> tuple[Path, _MutatingReviewer, str, str]:
    repo = make_repo(tmp_path)
    provider = _MutatingReviewer(repo, "reviewer_change.txt", commit=True)
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    assert approved
    provider.arm()
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.REVIEW_MUTATION_ERROR
    observed = head_commit(repo)
    assert observed != approved
    return repo, provider, approved, observed


def _plan_reviewer_mutation(repo: Path, provider: _MutatingReviewer) -> None:
    provider.arm()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.REVIEW_MUTATION_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is not None
    assert state.next_session == "planner"


def test_final_reviewer_commit_is_fail_safe_and_resume_reconciles(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _MutatingReviewer(repo, "reviewer_change.txt", commit=True)
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.LIMIT_REACHED
    approved_state = load_lifecycle_state(repo)
    assert approved_state is not None
    approved = approved_state.last_approved_commit
    assert approved
    evidence = [item.model_dump(mode="json") for item in approved_state.approved_evidence]
    sessions = _session_ids(repo)

    provider.arm()
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    assert outcome.message is not None
    assert "Reviewer product mutation detected." in outcome.message
    assert "Review result discarded." in outcome.message
    assert "State preserved." in outcome.message
    assert "auto-loop resume" in outcome.message

    state = load_lifecycle_state(repo)
    assert state is not None
    observed = head_commit(repo)
    assert observed != approved
    assert state.last_approved_commit == approved
    assert [item.model_dump(mode="json") for item in state.approved_evidence] == evidence
    assert state.active_review is None
    assert state.completed_provider_turn is None
    assert state.next_session == "worker"
    assert _session_ids(repo) == sessions
    recovery = state.review_mutation_recovery
    assert recovery is not None
    assert recovery.reconciled is False
    assert recovery.scope == "final"
    assert recovery.target == "whole-task"
    assert recovery.reviewer_slot == "reviewer"
    assert recovery.implementer_slot == "worker"
    assert recovery.expected_head == approved
    assert recovery.observed_head == observed
    assert recovery.suspended_review is not None
    assert recovery.suspended_review.current_candidate_head == approved
    assert "review_mutation_detected" in _event_types(repo)

    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review mutation recovery:" in report
    assert "scope: final" in report
    assert "target: whole-task" in report
    assert f"expected HEAD: {approved[:7]}" in report
    assert f"observed HEAD: {observed[:7]}" in report
    assert "next session: worker" in report
    assert "active review:" not in report

    _inject_stale_reviewer_attempt(repo, summary="STALE VERDICT")
    start = len(provider.engine.invocations)
    provider.set_worker_final_request()
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    resumed = run_lifecycle(repo, run_opts(6, resuming=True), provider)
    assert resumed.exit_code == ExitCode.COMPLETE
    new = provider.engine.invocations[start:]
    assert new[0].role == "worker"
    assert "Re-emit the result only" not in new[0].prompt
    assert f"expected candidate HEAD: {approved[:7]}" in new[0].prompt
    assert f"observed HEAD: {observed[:7]}" in new[0].prompt
    assert f"HEAD: {observed[:7]}" in new[0].prompt
    assert "Do not assume the previous reviewer verdict remains valid." in new[0].prompt
    assert "request a fresh batch/final review" in new[0].prompt
    assert any("Final handoff rejected." in inv.prompt for inv in new if inv.role == "worker")
    assert any(
        "Submit those changes for batch review" in inv.prompt for inv in new if inv.role == "worker"
    )
    assert "STALE VERDICT" not in latest_review_text(repo)
    finished = load_lifecycle_state(repo)
    assert finished is not None
    assert finished.review_mutation_recovery is None
    assert finished.last_approved_commit == observed
    assert finished.active_review is None
    completion = load_completion_record(repo)
    assert completion is not None
    assert completion.final_commit == observed
    assert _session_ids(repo) == sessions
    types = _event_types(repo)
    assert "review_mutation_recovery_started" in types
    assert "review_mutation_review_invalidated" in types
    assert "review_mutation_returned_to_worker" in types


def test_restored_final_candidate_gets_a_fresh_reviewer_inspection(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _MutatingReviewer(repo, "reviewer_change.txt", commit=True)
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    assert approved
    sessions = _session_ids(repo)
    provider.arm()
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    git(repo, "reset", "--hard", approved)
    assert head_commit(repo) == approved
    _inject_stale_reviewer_attempt(repo, summary="STALE VERDICT")
    start = len(provider.engine.invocations)
    provider.set_reviewer_complete()
    resumed = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert resumed.exit_code == ExitCode.COMPLETE
    new = provider.engine.invocations[start:]
    assert new[0].role == "reviewer"
    assert "Do not reuse or merely re-emit your previous verdict." in new[0].prompt
    assert "Re-inspect the current repository" in new[0].prompt
    assert "Re-emit the result only" not in new[0].prompt
    assert "STALE VERDICT" not in latest_review_text(repo)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == approved
    assert state.review_mutation_recovery is None
    assert state.reviewer_retry_after_mutation is False
    assert _session_ids(repo) == sessions


def test_changed_path_target_returns_to_worker_until_restored(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("secret-report.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore report")
    provider = _MutatingReviewer(repo, "secret-report.txt", commit=False)
    approve_plan(repo, provider)
    original = "original-report\n"
    (repo / "secret-report.txt").write_text(original, encoding="utf-8")
    approved = load_lifecycle_state(repo).last_approved_commit
    provider.arm()
    provider.set_response(
        "worker",
        batch_worker_payload_with_path("secret-report.txt", path_id="report"),
    )
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == approved
    assert head_commit(repo) == approved
    assert state.active_review is None
    assert state.next_session == "worker"
    recovery = state.review_mutation_recovery
    assert recovery is not None
    assert recovery.expected_head == approved
    assert recovery.observed_head == approved
    sessions = _session_ids(repo)
    assert sessions["worker"] and sessions["reviewer"]

    start = len(provider.engine.invocations)
    provider.set_response(
        "worker",
        batch_worker_payload_with_path("secret-report.txt", path_id="report"),
    )
    changed = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert changed.exit_code == ExitCode.LIMIT_REACHED
    new = provider.engine.invocations[start:]
    assert new[0].role == "worker"
    assert all(inv.role != "reviewer" for inv in new)
    assert "Do not assume the previous reviewer verdict remains valid." in new[0].prompt
    routed = load_lifecycle_state(repo)
    assert routed is not None
    assert routed.last_approved_commit == approved
    assert routed.active_review is not None
    assert routed.active_review.current_candidate_head == approved
    path_targets = [target for target in routed.active_review.targets if target.kind == "path"]
    assert path_targets
    assert "reviewer-mutation" in (repo / "secret-report.txt").read_text(encoding="utf-8")
    assert routed.review_mutation_recovery is None

    restored_root = tmp_path / "restored"
    restored_root.mkdir()
    repo = make_repo(restored_root)
    (repo / ".gitignore").write_text("secret-report.txt\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore report")
    provider = _MutatingReviewer(repo, "secret-report.txt", commit=False)
    approve_plan(repo, provider)
    (repo / "secret-report.txt").write_text(original, encoding="utf-8")
    approved = load_lifecycle_state(repo).last_approved_commit
    provider.arm()
    provider.set_response(
        "worker",
        batch_worker_payload_with_path("secret-report.txt", path_id="report"),
    )
    provider.set_reviewer_pass("batch", "W01")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.REVIEW_MUTATION_ERROR
    sessions = _session_ids(repo)
    (repo / "secret-report.txt").write_text(original, encoding="utf-8")
    _inject_stale_reviewer_attempt(repo, summary="STALE VERDICT")
    start = len(provider.engine.invocations)
    provider.set_response(
        "reviewer",
        {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "batch",
            "target": "W01",
            "reviewed_target_ids": ["report"],
            "summary": "fresh path review",
            "findings": [],
            "verification": [],
        },
    )
    restored = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert restored.exit_code == ExitCode.LIMIT_REACHED
    new = provider.engine.invocations[start:]
    assert new[0].role == "reviewer"
    assert "Do not reuse or merely re-emit your previous verdict." in new[0].prompt
    assert "Re-emit the result only" not in new[0].prompt
    review = latest_review_text(repo)
    assert "fresh path review" in review
    assert "STALE VERDICT" not in review
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == approved
    assert _session_ids(repo) == sessions


def test_plan_reviewer_mutation_returns_to_planner_until_restored(tmp_path: Path):
    repo = make_repo(tmp_path)
    plan_rel = ".ai/auto-loop/plan.md"
    original = (repo / plan_rel).read_text(encoding="utf-8")
    provider = _MutatingReviewer(repo, plan_rel, commit=False)
    provider.arm()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.phase == "planning"
    assert state.plan_approved is False
    assert state.next_session == "planner"
    assert state.active_review is None
    recovery = state.review_mutation_recovery
    assert recovery is not None
    assert recovery.reviewer_slot == "plan_reviewer"
    assert recovery.implementer_slot == "planner"
    sessions = _session_ids(repo)
    assert sessions["planner"]
    assert sessions["plan_reviewer"]

    start = len(provider.engine.invocations)
    provider.set_planner_review_request()
    changed = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert changed.exit_code == ExitCode.LIMIT_REACHED
    new = provider.engine.invocations[start:]
    assert new[0].role == "planner"
    assert all(inv.role != "plan_reviewer" for inv in new)
    assert "previous plan-review verdict remains valid" in new[0].prompt
    assert "Do not implement product changes." in new[0].prompt
    assert "Do not repair or revert product Git state." in new[0].prompt
    assert "repair or revert it before" not in new[0].prompt.lower()
    assert "Product-state reconciliation must happen outside the planner." in new[0].prompt
    assert _session_ids(repo)["planner"] == sessions["planner"]
    assert _session_ids(repo)["plan_reviewer"] == sessions["plan_reviewer"]

    restored_root = tmp_path / "restored-plan"
    restored_root.mkdir()
    repo = make_repo(restored_root)
    original = (repo / plan_rel).read_text(encoding="utf-8")
    provider = _MutatingReviewer(repo, plan_rel, commit=False)
    provider.arm()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.REVIEW_MUTATION_ERROR
    (repo / plan_rel).write_text(original, encoding="utf-8")
    sessions = _session_ids(repo)
    _inject_stale_reviewer_attempt(repo, summary="STALE VERDICT")
    start = len(provider.engine.invocations)
    provider.set_reviewer_pass("plan", "plan")
    restored = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert restored.exit_code == ExitCode.LIMIT_REACHED
    new = provider.engine.invocations[start:]
    assert new[0].role == "plan_reviewer"
    assert "Do not reuse or merely re-emit your previous verdict." in new[0].prompt
    assert "Re-emit the result only" not in new[0].prompt
    assert "STALE VERDICT" not in latest_review_text(repo)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.phase == "execution"
    assert _session_ids(repo)["planner"] == sessions["planner"]
    assert _session_ids(repo)["plan_reviewer"] == sessions["plan_reviewer"]


def test_mutation_recovery_cleared_when_worker_reports_waiting(tmp_path: Path):
    repo, provider, _approved, _observed = _final_mutation_with_changed_head(tmp_path)
    config = _suspend_config(repo)
    clock = FakeClock()
    reason = "CI is running for the pushed closure SHA"
    provider.set_worker_waiting(reason)
    provider.set_reviewer_pass("batch", "waiting")
    outcome = run_lifecycle(
        repo, run_opts(4, resuming=True), provider, config=config, clock=clock
    )
    assert outcome.exit_code == ExitCode.WAITING
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is None
    assert state.status == LifecycleStatus.WAITING
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review mutation recovery:" not in report
    assert "Status: WAITING" in report

    clock.advance(60)
    provider.set_worker_waiting(reason)
    start = len(provider.engine.invocations)
    run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    worker_prompts = [
        inv.prompt for inv in provider.engine.invocations[start:] if inv.role == "worker"
    ]
    assert worker_prompts
    assert "You previously reported WAITING" in worker_prompts[0]
    assert "previous reviewer turn was discarded" not in worker_prompts[0].lower()


def test_mutation_recovery_cleared_when_worker_reports_blocked(tmp_path: Path):
    repo, provider, _approved, _observed = _final_mutation_with_changed_head(tmp_path)
    config = _suspend_config(repo)
    config.run.require_blocker_review = False
    provider.set_worker_blocked("hosted deployment approval is missing")
    outcome = run_lifecycle(
        repo, run_opts(2, resuming=True), provider, config=config
    )
    assert outcome.exit_code == ExitCode.BLOCKED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is None
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review mutation recovery:" not in report
    assert "blocked:" in report


def test_mutation_recovery_cleared_when_planner_reports_waiting(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _MutatingReviewer(repo, ".ai/auto-loop/plan.md", commit=False)
    _plan_reviewer_mutation(repo, provider)
    config = _suspend_config(repo)
    clock = FakeClock()
    reason = "external spec review is already in progress"
    provider.set_planner_waiting(reason)
    provider.set_reviewer_pass("plan", "waiting", slot="plan_reviewer")
    outcome = run_lifecycle(
        repo, run_opts(4, resuming=True), provider, config=config, clock=clock
    )
    assert outcome.exit_code == ExitCode.WAITING
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is None
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review mutation recovery:" not in report

    clock.advance(60)
    provider.set_planner_waiting(reason)
    start = len(provider.engine.invocations)
    run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    planner_prompts = [
        inv.prompt for inv in provider.engine.invocations[start:] if inv.role == "planner"
    ]
    assert planner_prompts
    assert "You previously reported WAITING" in planner_prompts[0]
    assert "previous plan-reviewer turn was discarded" not in planner_prompts[0].lower()


def test_mutation_recovery_cleared_when_planner_reports_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _MutatingReviewer(repo, ".ai/auto-loop/plan.md", commit=False)
    _plan_reviewer_mutation(repo, provider)
    config = frozen_config(repo)
    config.run.require_blocker_review = False
    provider.set_planner_blocked("external planning approval required")
    outcome = run_lifecycle(repo, run_opts(2, resuming=True), provider, config=config)
    assert outcome.exit_code == ExitCode.BLOCKED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.review_mutation_recovery is None
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "review mutation recovery:" not in report
    assert "blocked:" in report
