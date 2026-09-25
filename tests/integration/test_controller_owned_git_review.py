"""Controller-owned Git batch review ranges and worker target contract."""

from __future__ import annotations

from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.git import resolve_commit
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


def test_committed_batch_without_targets_derives_exact_git_range_for_reviewer(tmp_path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    baseline = state.last_approved_commit
    assert baseline
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    base_resolved = resolve_commit(repo, baseline)
    head_resolved = resolve_commit(repo, head)
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("reviewer", _reviewer_pass_batch())
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.active_review is None
    assert resolve_commit(repo, state.last_approved_commit) == head_resolved
    assert provider.reviewer_prompts
    reviewer_prompt = provider.reviewer_prompts[-1]
    assert f"- git: Git range {base_resolved}..{head_resolved}" in reviewer_prompt


def test_legacy_git_range_target_still_parses_and_reviewer_gets_git_target(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    base_resolved = resolve_commit(repo, baseline)
    head_resolved = resolve_commit(repo, head)
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
    assert len([inv for inv in provider.engine.invocations if inv.role == "reviewer"]) == 1
    assert resolve_commit(repo, load_lifecycle_state(repo).last_approved_commit) == head_resolved
    assert f"{base_resolved}..{head_resolved}" in provider.reviewer_prompts[-1]
