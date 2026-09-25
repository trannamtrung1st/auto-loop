"""Reviewer result must match the controller's active review."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.exits import ExitCode
from auto_loop.protocol import ProtocolParseError
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import (
    ActiveReview,
    CompletedProviderTurn,
    PendingRevision,
    create_lifecycle,
)
from auto_loop.runtime import save_lifecycle_state
from auto_loop.loop import LifecycleRunner
from auto_loop.models import ActiveGitTarget, ReviewerResult
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state

from tests.integration.scenario_harness import (
    approve_plan,
    batch_worker_payload,
    commit_file,
    make_repo,
    run_lifecycle,
    run_opts,
)
from auto_loop.providers.scripted import ScriptedProvider


def _runner(tmp_path: Path) -> LifecycleRunner:
    repo = tmp_path / "r"
    repo.mkdir()
    bootstrap_workspace(repo)
    config = default_config()
    return LifecycleRunner(
        repo,
        config,
        RunOptions("auto", "auto", max_turns=5, max_runtime_minutes=60, verbose=False, quiet=True),
        ScriptedProvider(),
    )


def test_plan_reviewer_complete_is_protocol_parse_error(tmp_path: Path):
    runner = _runner(tmp_path)
    state = create_lifecycle("abc")
    state.active_review = ActiveReview(
        cycle_id="c1",
        scope="plan",
        target="plan",
        summary="s",
        session_purpose="plan_reviewer",
    )
    result = ReviewerResult.model_validate(
        {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "complete",
            "scope": "final",
            "target": "whole-task",
            "whole_task_reviewed": True,
            "summary": "done",
            "findings": [],
            "verification": [],
            "reviewed_target_ids": ["plan"],
        }
    )
    with pytest.raises(ProtocolParseError, match="Plan reviewer cannot declare"):
        runner._validate_reviewer_handoff(state, "plan_reviewer", result)


def test_assert_reviewer_matches_active_scope_and_target(tmp_path: Path):
    runner = _runner(tmp_path)
    active = ActiveReview(cycle_id="c1", scope="batch", target="W01", summary="s")
    ok = ReviewerResult.model_validate(
        {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "batch",
            "target": "W01",
            "summary": "ok",
            "findings": [],
            "verification": [],
            "reviewed_target_ids": ["git"],
        }
    )
    runner._assert_reviewer_matches_active(active, ok)
    bad = ok.model_copy(update={"target": "W02"})
    with pytest.raises(ProtocolParseError, match="target"):
        runner._assert_reviewer_matches_active(active, bad)


def test_assert_reviewer_rejects_wrong_git_commits(tmp_path: Path):
    repo = make_repo(tmp_path)
    base = commit_file(repo, "a.txt", "a\n", "a")
    head = commit_file(repo, "b.txt", "b\n", "b")
    wrong = commit_file(repo, "c.txt", "c\n", "c")
    runner = LifecycleRunner(
        repo,
        default_config(),
        run_opts(2),
        ScriptedProvider(),
    )
    active = ActiveReview(
        cycle_id="c1",
        scope="batch",
        target="W01",
        summary="s",
        targets=[ActiveGitTarget(base_commit=base, head_commit=head)],
    )
    result = ReviewerResult.model_validate(
        {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "batch",
            "target": "W01",
            "summary": "ok",
            "findings": [],
            "verification": [],
            "reviewed_target_ids": ["git"],
            "reviewed_base_commit": base,
            "reviewed_head_commit": wrong,
        }
    )
    with pytest.raises(ProtocolParseError, match="reviewed_head_commit"):
        runner._assert_reviewer_matches_active(active, result)


def test_integration_wrong_batch_target_repairs_in_same_run(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    commit_file(repo, "f.txt", "x\n", "f")
    provider.set_response("worker", batch_worker_payload("W01"))
    provider.set_reviewer_pass("batch", "WRONG")
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    reviewer_invocations = [inv for inv in provider.engine.invocations if inv.role == "reviewer"]
    assert len(reviewer_invocations) == 2
    reviewer_session = state.sessions["reviewer"].session_id
    assert reviewer_session
    assert reviewer_invocations[1].resume_session_id == reviewer_session


def test_plan_reviewer_wrong_plan_target_repairs_in_same_run(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "not-plan", slot="plan_reviewer")
    provider.set_reviewer_pass("plan", "plan", slot="plan_reviewer")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved
    assert state.consecutive_protocol_failures == 0
    plan_reviewer_invocations = [
        inv for inv in provider.engine.invocations if inv.role == "plan_reviewer"
    ]
    assert len(plan_reviewer_invocations) == 2
    session_id = state.sessions["plan_reviewer"].session_id
    assert session_id
    assert all(inv.resume_session_id in (None, session_id) for inv in plan_reviewer_invocations)


def test_reviewer_binding_mismatch_exhausts_protocol_retries(tmp_path: Path):
    from tests.repo_utils import frozen_config

    repo = make_repo(tmp_path)
    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"run": cfg.run.model_copy(update={"protocol_retries": 1})})

    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "wrong-1", slot="plan_reviewer")
    provider.set_reviewer_pass("plan", "wrong-2", slot="plan_reviewer")

    outcome = run_lifecycle(repo, run_opts(2), provider, config=cfg)
    assert outcome.exit_code == ExitCode.PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.consecutive_protocol_failures == 2
    assert state.last_run_failure is not None
    assert state.last_run_failure.session == "plan_reviewer"
    assert state.inflight is not None
    assert state.inflight.repair_reason
    assert state.next_session == "plan_reviewer"
    plan_reviewer_invocations = [
        inv for inv in provider.engine.invocations if inv.role == "plan_reviewer"
    ]
    assert len(plan_reviewer_invocations) == 2


def test_legacy_completed_reviewer_turn_repairs_in_same_resume(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    commit_file(repo, "f.txt", "x\n", "f")
    provider.set_response("worker", batch_worker_payload("W01"))
    run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.active_review is not None
    assert state.next_session == "reviewer"
    reviewer_session = state.sessions["reviewer"].session_id or "legacy-reviewer-session"
    state.sessions["reviewer"].session_id = reviewer_session
    provider.engine.sessions["reviewer"] = reviewer_session
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="reviewer",
        role="reviewer",
        turn=state.turn,
        session_id=reviewer_session,
        result_kind="reviewer",
        result={
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "batch",
            "target": "WRONG",
            "summary": "ok",
            "findings": [],
            "verification": [],
            "reviewed_target_ids": ["git"],
        },
    )
    state.inflight = None
    save_lifecycle_state(repo, state)

    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    assert state.consecutive_protocol_failures == 0
    reviewer_invocations = [inv for inv in provider.engine.invocations if inv.role == "reviewer"]
    assert len(reviewer_invocations) == 1
    assert reviewer_invocations[0].resume_session_id == reviewer_session


def test_planning_review_cycle_reuses_pending_revision(tmp_path: Path):
    runner = _runner(tmp_path)
    state = create_lifecycle("abc")
    state.pending_revision = PendingRevision(
        cycle_id="review-0003",
        scope="plan",
        target="plan",
        round=2,
        finding_review_file=".ai/auto-loop/reviews/x.md",
    )
    cycle_id, round_no = runner._planning_review_cycle(state, "plan")
    assert cycle_id == "review-0003"
    assert round_no == 2
