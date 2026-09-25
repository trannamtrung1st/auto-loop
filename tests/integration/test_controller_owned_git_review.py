"""Controller-owned Git batch review ranges and worker target contract."""

from __future__ import annotations

from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.runtime import load_lifecycle_state
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    batch_worker_payload,
    commit_file,
    make_repo,
    run_lifecycle,
    run_opts,
)
from tests.integration.test_lifecycle_flows import _approve_plan


def _reviewer_pass_batch(target: str = "W01") -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": "batch",
        "target": target,
        "reviewed_target_ids": ["git"],
        "summary": "ok",
        "findings": [],
        "verification": [],
    }


def test_committed_batch_without_targets_derives_git_range_for_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("reviewer", _reviewer_pass_batch())
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.active_review is None
    reviews = sorted((repo / ".ai" / "auto-loop" / "reviews").glob("*.md"))
    assert reviews
    assert ".." in reviews[-1].read_text(encoding="utf-8")


def test_legacy_git_range_target_still_parses_and_reviewer_gets_git_target(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    payload = batch_worker_payload()
    payload["review"]["targets"] = [
        {
            "kind": "git_range",
            "id": "git",
            "base_commit": baseline,
            "head_commit": head,
        }
    ]
    provider.set_response("worker", payload)
    provider.set_response("reviewer", _reviewer_pass_batch())
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    reviewer_invocations = [inv for inv in provider.engine.invocations if inv.role == "reviewer"]
    assert len(reviewer_invocations) == 1


