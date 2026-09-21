"""Reviewer result must match the controller's active review."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.exits import ExitCode
from auto_loop.git import GitProtocolError
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import ActiveReview, PendingRevision, create_lifecycle
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
    with pytest.raises(GitProtocolError, match="target"):
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
    with pytest.raises(GitProtocolError, match="reviewed_head_commit"):
        runner._assert_reviewer_matches_active(active, result)


def test_integration_wrong_batch_target_is_git_protocol_error(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "f.txt", "x\n", "f")
    provider.set_response("worker", batch_worker_payload(baseline, head, "W01"))
    provider.set_reviewer_pass("batch", "WRONG")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


def test_plan_reviewer_wrong_plan_target_rejected(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "not-plan", slot="plan_reviewer")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


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
