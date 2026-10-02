"""Execution-time replanning stays distinct from blocked, waiting, and product approval."""

from __future__ import annotations

import os
from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import CompletedProviderTurn, LifecycleStatus
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.status_report import build_status_report
from tests.integration.scenario_harness import (
    approve_plan,
    batch_worker_payload,
    commit_file,
    git,
    latest_review_text,
    make_repo,
    run_lifecycle,
    run_opts,
)
from tests.repo_utils import frozen_config


def _plan_followup() -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {"scope": "plan", "target": "plan", "summary": "continue from the revised plan"},
        "work_summary": "Read the revised plan.",
        "verification": [],
        "notes": [],
    }


def _events(repo: Path) -> list[str]:
    return [event.get("type", "") for event in load_events(repo, frozen_config(repo))]


def _state(repo: Path):
    state = load_lifecycle_state(repo)
    assert state is not None
    return state


def _invocations(provider: ScriptedProvider, role: str, *, start: int = 0):
    return [inv for inv in provider.engine.invocations[start:] if inv.role == role]


class _DriftingReplanPlanner(ScriptedProvider):
    """Commit product work on the first replanning planner turn and leave it there."""

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.drifted = False

    def invoke(self, argv: list[str]):
        slot = os.environ.get("AUTO_LOOP_FAKE_SLOT")
        if slot == "planner" and not self.drifted and (self.repo / "feature.txt").exists():
            self.drifted = True
            path = self.repo / "unexpected.txt"
            path.write_text("drift\n", encoding="utf-8")
            git(self.repo, "add", "unexpected.txt")
            git(self.repo, "commit", "-m", "planner drift during replan")
        return super().invoke(argv)


def test_tactical_batch_does_not_replan(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "tactical\n", "tactical")
    start = len(provider.engine.invocations)
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.replan_context is None
    assert state.sessions["planner"].status == "retired"
    assert _invocations(provider, "planner", start=start) == []
    assert "replan_requested" not in _events(repo)


def test_worker_plan_update_stays_with_execution_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    start = len(provider.engine.invocations)
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {"scope": "plan", "target": "plan", "summary": "worker amended the plan"},
            "work_summary": "Updated the plan from implementation evidence.",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_reviewer_pass("plan", "plan", slot="reviewer")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.replan_context is None
    assert state.sessions["planner"].status == "retired"
    assert state.sessions["plan_reviewer"].status == "retired"
    assert _invocations(provider, "planner", start=start) == []
    assert _invocations(provider, "plan_reviewer", start=start) == []
    assert len(_invocations(provider, "reviewer", start=start)) == 1


def test_worker_replan_returns_to_same_worker_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = _state(repo)
    planner_id = approved.sessions["planner"].session_id
    plan_reviewer_id = approved.sessions["plan_reviewer"].session_id
    assert planner_id and plan_reviewer_id
    provider.set_worker_replan("P3 persistence assumption is false.", affected=["P3", "P4"])
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _plan_followup())
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.replan_context is None
    assert state.replan_resume is None
    assert state.last_approved_commit == approved.last_approved_commit
    assert state.sessions["planner"].session_id == planner_id
    assert state.sessions["plan_reviewer"].session_id == plan_reviewer_id
    assert state.sessions["planner"].status == "retired"
    workers = _invocations(provider, "worker")
    assert len(workers) == 2
    assert workers[1].resume_session_id == state.sessions["worker"].session_id
    assert "Execution-time replanning completed." in workers[1].prompt
    review = latest_review_text(repo)
    assert "Replan cycle: `replan-0001`" in review
    assert "P3 persistence assumption is false." in review
    planner = _invocations(provider, "planner")
    assert planner[-1].resume_session_id == planner_id
    assert "execution-time replan" in planner[-1].prompt
    assert "replan_approved" in _events(repo)
    assert state.initial_approved_plan_sha256 == approved.initial_approved_plan_sha256


def test_replan_revise_reuses_planner_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    planner_id = _state(repo).sessions["planner"].session_id
    provider.set_worker_replan()
    provider.set_planner_review_request()
    provider.set_reviewer_revise("plan", "plan", slot="plan_reviewer")
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    outcome = run_lifecycle(repo, run_opts(5), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    planners = _invocations(provider, "planner")
    assert len(planners) == 3
    assert planners[-1].resume_session_id == planner_id
    assert planners[-2].resume_session_id == planner_id
    assert "replan_revised" in _events(repo)
    assert _state(repo).phase == "execution"
    assert _state(repo).next_session == "worker"


def test_replan_preserves_unapproved_commits_for_later_batch_review(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved_head = _state(repo).last_approved_commit
    assert approved_head
    commit_file(repo, "feature.txt", "b\n", "B")
    head_c = commit_file(repo, "feature.txt", "c\n", "C")
    provider.set_worker_replan()
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    run_lifecycle(repo, run_opts(3), provider)
    state = _state(repo)
    assert state.last_approved_commit == approved_head
    assert head_commit(repo) == head_c
    assert state.phase == "execution"
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    reviewed = run_lifecycle(repo, run_opts(2), provider)
    assert reviewed.exit_code == ExitCode.LIMIT_REACHED
    assert _state(repo).last_approved_commit == head_c
    assert f"{approved_head}..{head_c}" in latest_review_text(repo)


def test_replan_freezes_dirty_product_tree(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    head = head_commit(repo)
    dirty = repo / "partial.txt"
    dirty.write_text("partial implementation\n", encoding="utf-8")
    provider.set_worker_replan()
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _plan_followup())
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert head_commit(repo) == head
    assert dirty.read_text(encoding="utf-8") == "partial implementation\n"
    assert _state(repo).last_approved_commit == head
    planner = _invocations(provider, "planner")[-1]
    assert "Do not modify product files." in planner.prompt
    assert "Do not reset the repository to the initial planning baseline." in planner.prompt
    resumed = _invocations(provider, "worker")[-1]
    assert "Execution-time replanning completed." in resumed.prompt
    assert head in resumed.prompt
    assert "dirty" in resumed.prompt


def test_planner_product_mutation_during_replan_returns_to_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _DriftingReplanPlanner(repo)
    approve_plan(repo, provider)
    approved = _state(repo).last_approved_commit
    commit_file(repo, "feature.txt", "started\n", "started")
    provider.set_worker_replan()
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.next_session == "worker"
    assert state.replan_context is None
    assert state.replan_invalidation is not None
    assert state.last_approved_commit == approved
    assert state.plan_approved is True
    assert "replan_invalidated" in _events(repo)
    assert "replan_approved" not in _events(repo)
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "replan reconciliation" in report
    provider.set_worker_replan("Request a fresh strategy after the stale snapshot.")
    recovered = run_lifecycle(repo, run_opts(1), provider)
    assert recovered.exit_code == ExitCode.LIMIT_REACHED
    recovered_state = _state(repo)
    assert recovered_state.phase == "replanning"
    assert recovered_state.replan_seq == 2
    assert recovered_state.replan_context is not None
    assert recovered_state.last_approved_commit == approved


def test_external_product_change_during_replan_is_not_trusted(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = _state(repo).last_approved_commit
    provider.set_worker_replan()
    provider.set_planner_review_request()
    started = run_lifecycle(repo, run_opts(2), provider)
    assert started.exit_code == ExitCode.LIMIT_REACHED
    assert _state(repo).phase == "replanning"
    assert _state(repo).next_session == "plan_reviewer"
    commit_file(repo, "external.txt", "changed\n", "external edit")
    start = len(provider.engine.invocations)
    provider.set_response("worker", _plan_followup())
    resumed = run_lifecycle(repo, run_opts(2), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert _invocations(provider, "plan_reviewer", start=start) == []
    state = _state(repo)
    assert state.phase == "execution"
    assert state.next_session == "reviewer"
    assert state.last_approved_commit == approved
    assert state.replan_context is None
    assert state.replan_invalidation is None
    worker = _invocations(provider, "worker", start=start)
    assert worker
    assert "Replan evidence changed while planning." in worker[0].prompt
    assert "replan_invalidated" in _events(repo)


def test_resume_after_replan_request_does_not_duplicate_the_cycle(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_replan()
    first = run_lifecycle(repo, run_opts(1), provider)
    assert first.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "replanning"
    assert state.replan_seq == 1
    assert state.replan_context is not None
    assert state.replan_context.cycle_id == "replan-0001"
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "Phase: REPLANNING" in report
    assert "Requested by: worker" in report
    assert state.replan_context.reason in report
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _plan_followup())
    resumed = run_lifecycle(repo, run_opts(3), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert _state(repo).replan_seq == 1
    assert _events(repo).count("replan_started") == 1
    assert _events(repo).count("replan_requested") == 1


def test_replay_of_completed_replan_request_reuses_the_cycle(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_replan(affected=["P3"])
    run_lifecycle(repo, run_opts(1), provider)
    state = _state(repo)
    assert state.replan_context is not None
    requested_turn = state.replan_context.requested_at_turn
    state.phase = "execution"
    state.next_session = "worker"
    state.turn = requested_turn
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="worker",
        role="worker",
        turn=requested_turn,
        session_id=state.sessions["worker"].session_id,
        result_kind="worker",
        result={
            "schema_version": 2,
            "actor": "worker",
            "status": "replan_requested",
            "replan": {
                "reason": state.replan_context.reason,
                "evidence": state.replan_context.evidence,
                "affected_plan_items": ["P3"],
                "safe_to_keep": ["P1"],
                "suggested_direction": "Revise the affected plan items.",
            },
            "work_summary": state.replan_context.reason,
            "verification": [],
            "notes": [],
        },
    )
    save_lifecycle_state(repo, state)
    provider.set_planner_review_request()
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    replayed = _state(repo)
    assert replayed.replan_seq == 1
    assert replayed.replan_context is not None
    assert replayed.replan_context.cycle_id == "replan-0001"
    assert replayed.phase == "replanning"
    assert _events(repo).count("replan_started") == 1


def test_interrupted_replan_pass_resumes_same_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_replan()
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    stopped = run_lifecycle(repo, run_opts(3), provider)
    assert stopped.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.next_session == "worker"
    assert state.replan_resume is not None
    assert state.replan_context is None
    start = len(provider.engine.invocations)
    provider.set_response("worker", _plan_followup())
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    worker = _invocations(provider, "worker", start=start)
    assert len(worker) == 1
    assert "Execution-time replanning completed." in worker[0].prompt
    assert worker[0].resume_session_id == _state(repo).sessions["worker"].session_id


def test_blocked_and_waiting_do_not_start_a_replan(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked("credentials must be supplied")
    provider.set_reviewer_blocked("credentials must be supplied")
    blocked = run_lifecycle(repo, run_opts(2), provider)
    assert blocked.exit_code == ExitCode.BLOCKED
    assert _state(repo).replan_context is None
    assert "replan_requested" not in _events(repo)

    waiting_root = tmp_path / "waiting"
    waiting_root.mkdir()
    waiting_repo = make_repo(waiting_root)
    waiting = ScriptedProvider()
    approve_plan(waiting_repo, waiting)
    config = frozen_config(waiting_repo)
    config.run.wait_mode = "suspend"
    waiting.set_worker_waiting("remote CI is already running")
    waiting.set_reviewer_pass("batch", "waiting")
    outcome = run_lifecycle(waiting_repo, run_opts(2), waiting, config=config)
    assert outcome.exit_code == ExitCode.WAITING
    assert _state(waiting_repo).status == LifecycleStatus.WAITING
    assert _state(waiting_repo).replan_context is None
    assert "replan_requested" not in _events(waiting_repo)


def test_operator_plan_edit_is_reviewed_before_the_worker_resumes(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    plan = repo / ".ai" / "auto-loop" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nOperator amendment.\n", encoding="utf-8")
    approved_head = _state(repo).last_approved_commit
    start = len(provider.engine.invocations)
    provider.set_reviewer_revise("plan", "plan", slot="plan_reviewer")
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _plan_followup())
    outcome = run_lifecycle(repo, run_opts(5), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    worker = _invocations(provider, "worker", start=start)
    assert "An operator changed the plan outside the lifecycle." in worker[-1].prompt
    assert "operator_plan_change_detected" in _events(repo)
    planners = _invocations(provider, "planner", start=start)
    assert len(planners) == 1
    state = _state(repo)
    assert state.phase == "execution"
    assert state.plan_approved is True
    assert state.last_approved_commit == approved_head
    assert head_commit(repo) == approved_head


def _planner_review_result() -> dict:
    return {
        "schema_version": 2,
        "actor": "planner",
        "status": "review_requested",
        "review": {"scope": "plan", "target": "plan", "summary": "amended plan"},
        "plan_summary": "Revised the affected items.",
        "notes": [],
    }


def _plan_pass_result() -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": "plan",
        "target": "plan",
        "reviewed_target_ids": ["plan"],
        "summary": "amended plan accepted",
        "findings": [],
        "verification": [],
    }


def test_replay_completed_planner_result_does_not_invoke_the_planner_again(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = _state(repo).last_approved_commit
    provider.set_worker_replan()
    run_lifecycle(repo, run_opts(1), provider)
    state = _state(repo)
    assert state.replan_context is not None
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id=state.sessions["planner"].session_id,
        result_kind="planner",
        result=_planner_review_result(),
        product_head_before=state.replan_context.product_head,
        product_changes_before=[list(row) for row in state.replan_context.product_rows],
    )
    state.next_session = "planner"
    state.inflight = None
    save_lifecycle_state(repo, state)
    start = len(provider.engine.invocations)
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert _invocations(provider, "planner", start=start) == []
    replayed = _state(repo)
    assert replayed.phase == "replanning"
    assert replayed.next_session == "plan_reviewer"
    assert replayed.replan_seq == 1
    assert replayed.active_review is not None
    assert replayed.active_review.replan_cycle_id == "replan-0001"
    assert replayed.last_approved_commit == approved
    assert _events(repo).count("replan_review_requested") == 1


def test_resume_at_plan_review_keeps_one_cycle(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = _state(repo).last_approved_commit
    provider.set_worker_replan()
    provider.set_planner_review_request()
    started = run_lifecycle(repo, run_opts(2), provider)
    assert started.exit_code == ExitCode.LIMIT_REACHED
    assert _state(repo).next_session == "plan_reviewer"
    assert _state(repo).active_review is not None
    start = len(provider.engine.invocations)
    provider.set_plan_reviewer_pass()
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert len(_invocations(provider, "plan_reviewer", start=start)) == 1
    assert _invocations(provider, "planner", start=start) == []
    state = _state(repo)
    assert state.phase == "execution"
    assert state.next_session == "worker"
    assert state.replan_seq == 1
    assert state.replan_resume is not None
    assert state.last_approved_commit == approved
    assert "Replan cycle: `replan-0001`" in latest_review_text(repo)


def test_replay_completed_plan_review_pass_returns_to_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = _state(repo).last_approved_commit
    provider.set_worker_replan()
    provider.set_planner_review_request()
    run_lifecycle(repo, run_opts(2), provider)
    state = _state(repo)
    assert state.next_session == "plan_reviewer"
    assert state.active_review is not None
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="plan_reviewer",
        role="reviewer",
        turn=state.turn,
        session_id=state.sessions["plan_reviewer"].session_id,
        result_kind="reviewer",
        result=_plan_pass_result(),
    )
    state.inflight = None
    save_lifecycle_state(repo, state)
    start = len(provider.engine.invocations)
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert _invocations(provider, "plan_reviewer", start=start) == []
    assert _invocations(provider, "planner", start=start) == []
    replayed = _state(repo)
    assert replayed.phase == "execution"
    assert replayed.next_session == "worker"
    assert replayed.replan_context is None
    assert replayed.replan_resume is not None
    assert replayed.last_approved_commit == approved
    assert replayed.sessions["worker"].session_id


def test_resume_after_replan_revise_reuses_the_planner(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_replan()
    provider.set_planner_review_request()
    provider.set_reviewer_revise("plan", "plan", slot="plan_reviewer")
    revised = run_lifecycle(repo, run_opts(3), provider)
    assert revised.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "replanning"
    assert state.next_session == "planner"
    assert state.pending_revision is not None
    assert state.replan_seq == 1
    planner_id = state.sessions["planner"].session_id
    start = len(provider.engine.invocations)
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    resumed = run_lifecycle(repo, run_opts(2), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    planners = _invocations(provider, "planner", start=start)
    assert len(planners) == 1
    assert planners[0].resume_session_id == planner_id
    assert _state(repo).replan_seq == 1
    assert _state(repo).phase == "execution"
    assert _events(repo).count("replan_requested") == 1
    assert _events(repo).count("replan_revised") == 1


def test_replan_without_git_preserves_the_filesystem_snapshot(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    bootstrap_workspace(repo, git_mode="off")
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    approved = run_lifecycle(repo, run_opts(2), provider)
    assert approved.exit_code == ExitCode.LIMIT_REACHED
    partial = repo / "partial.txt"
    partial.write_text("partial\n", encoding="utf-8")
    provider.set_worker_replan("Filesystem evidence invalidated the strategy.")
    requested = run_lifecycle(repo, run_opts(1), provider)
    assert requested.exit_code == ExitCode.LIMIT_REACHED
    assert _state(repo).phase == "replanning"
    assert _state(repo).replan_context is not None
    assert _state(repo).replan_context.product_head is None
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _plan_followup())
    resumed = run_lifecycle(repo, run_opts(3), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    state = _state(repo)
    assert state.phase == "execution"
    assert state.replan_seq == 1
    assert state.last_approved_commit is None
    assert partial.read_text(encoding="utf-8") == "partial\n"
    assert "Execution-time replanning completed." in _invocations(provider, "worker")[-1].prompt
    assert _events(repo).count("replan_started") == 1
