"""Worker review-request repair when batch/final targets include control paths."""

from __future__ import annotations

from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.runtime import load_lifecycle_state
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    batch_worker_payload,
    batch_worker_payload_with_path,
    commit_file,
    execution_reviewer_invocation_count,
    make_repo,
    run_lifecycle,
    run_opts,
    worker_invocation_count,
    worker_session_ids,
)
from tests.integration.test_lifecycle_flows import _approve_plan


def _pass_batch() -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": "batch",
        "target": "W01",
        "reviewed_target_ids": ["git"],
        "summary": "ok",
        "findings": [],
        "verification": [],
    }


def test_escaping_path_target_repairs_without_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response(
        "worker",
        batch_worker_payload_with_path("../outside", path_id="escape"),
    )
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("reviewer", _pass_batch())
    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert execution_reviewer_invocation_count(provider._inner) == 1
    repair_prompts = [
        p
        for p in provider.worker_prompts
        if "escapes workspace" in p.lower() or "controller rejected" in p.lower()
    ]
    assert repair_prompts
    assert any("escapes workspace" in p.lower() for p in repair_prompts)


def test_batch_with_plan_path_repairs_in_run_and_opens_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    plan_rel = ".ai/auto-loop/plan.md"
    provider.set_response(
        "worker",
        batch_worker_payload_with_path(plan_rel, path_id="plan-bad"),
    )
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("reviewer", _pass_batch())
    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == head
    assert execution_reviewer_invocation_count(provider._inner) == 1
    assert worker_invocation_count(provider._inner) == 2
    worker_ids = [sid for sid in worker_session_ids(provider._inner) if sid]
    assert worker_ids
    assert state.sessions["worker"].session_id == worker_ids[-1]
    repair_prompts = [p for p in provider.worker_prompts if "controller rejected" in p.lower()]
    assert repair_prompts
    repair_prompt = repair_prompts[-1]
    assert "controller rejected" in repair_prompt.lower()
    assert "plan.md" in repair_prompt.lower()
    assert "batch/final" in repair_prompt.lower()


def test_batch_with_plan_path_resume_does_not_replay_rejected_turn(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    plan_rel = ".ai/auto-loop/plan.md"
    provider.set_response(
        "worker",
        batch_worker_payload_with_path(plan_rel, path_id="plan-bad"),
    )
    stopped = run_lifecycle(repo, run_opts(1), provider)
    assert stopped.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is None
    assert state.inflight is not None
    assert state.inflight.repair_reason
    assert "plan.md" in state.inflight.repair_reason
    worker_session_before = state.sessions["worker"].session_id
    assert worker_session_before
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("reviewer", _pass_batch())
    prompts_before = len(provider.worker_prompts)
    continued = run_lifecycle(repo, run_opts(2), provider)
    assert len(provider.worker_prompts) > prompts_before
    repair_prompt = provider.worker_prompts[-1]
    assert "controller rejected" in repair_prompt.lower()
    assert "plan.md" in repair_prompt.lower()
    assert continued.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.sessions["worker"].session_id == worker_session_before
    assert execution_reviewer_invocation_count(provider._inner) == 1
    assert state.last_approved_commit == head
