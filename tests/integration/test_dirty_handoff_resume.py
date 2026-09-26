"""Recoverable dirty-tree worker handoffs across resume/replay."""

from __future__ import annotations

from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.lifecycle import CompletedProviderTurn
from auto_loop.models import WorkerResult
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    approve_plan,
    batch_worker_payload,
    commit_file,
    execution_reviewer_invocation_count,
    git,
    make_repo,
    run_lifecycle,
    run_opts,
    worker_invocation_count,
)


class DirtyRepairCapturingProvider(PromptCapturingProvider):
    """Clears a dirty marker when the worker is re-invoked after handoff rejection."""

    def __init__(self, repo: Path, dirty_rel: str = "dirty.txt") -> None:
        super().__init__()
        self.repo = repo
        self.dirty_rel = dirty_rel

    def prepare(self, role: str) -> None:
        if role == "worker":
            dirty = self.repo / self.dirty_rel
            if dirty.is_file():
                dirty.unlink()
        super().prepare(role)


def _final_worker_payload() -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "final",
            "target": "whole-task",
            "summary": "requesting whole-task acceptance",
        },
        "work_summary": "ready for final review",
        "verification": [],
        "notes": [],
    }


def _approve_plan_and_batch(repo: Path, provider: PromptCapturingProvider) -> str:
    approve_plan(repo, provider)
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == head
    return head


def test_dirty_batch_completed_turn_resume_repairs_without_git_protocol_error(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    provider = DirtyRepairCapturingProvider(repo)
    approve_plan(repo, provider)
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    state = load_lifecycle_state(repo)
    assert state is not None
    worker_result = WorkerResult.model_validate(batch_worker_payload())
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="worker",
        role="worker",
        turn=state.turn,
        session_id=state.sessions["worker"].session_id,
        result_kind="worker",
        result=worker_result.model_dump(mode="json"),
        transition_error="Product working tree is not clean: dirty.txt",
    )
    state.inflight = None
    state.next_session = "worker"
    save_lifecycle_state(repo, state)

    provider.set_response("worker", batch_worker_payload())
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider)
    assert outcome.exit_code != ExitCode.GIT_PROTOCOL_ERROR
    assert execution_reviewer_invocation_count(provider) == 1
    reloaded = load_lifecycle_state(repo)
    assert reloaded is not None
    assert reloaded.last_approved_commit == head
    repair_prompts = [
        p
        for p in provider.worker_prompts
        if "handoff rejected" in p.lower() or "controller rejected" in p.lower()
    ]
    assert repair_prompts
    assert "dirty.txt" in repair_prompts[0]


def test_dirty_final_completed_turn_resume_repairs_without_git_protocol_error(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    provider = DirtyRepairCapturingProvider(repo)
    approved_head = _approve_plan_and_batch(repo, provider)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    state = load_lifecycle_state(repo)
    assert state is not None
    worker_result = WorkerResult.model_validate(_final_worker_payload())
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="worker",
        role="worker",
        turn=state.turn,
        session_id=state.sessions["worker"].session_id,
        result_kind="worker",
        result=worker_result.model_dump(mode="json"),
        transition_error="Product working tree is not clean: dirty.txt",
    )
    state.inflight = None
    state.next_session = "worker"
    save_lifecycle_state(repo, state)

    reviewers_before = execution_reviewer_invocation_count(provider)
    provider.set_response("worker", _final_worker_payload())
    provider.set_response("worker", _final_worker_payload())
    provider.set_reviewer_complete(approved_head)
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    repair_prompts = [
        p
        for p in provider.worker_prompts
        if "handoff rejected" in p.lower() or "controller rejected" in p.lower()
    ]
    assert repair_prompts
    assert "dirty.txt" in repair_prompts[0]
    assert execution_reviewer_invocation_count(provider) == reviewers_before + 1


def test_history_rewrite_on_resume_stays_fatal_not_dirty_handoff_repair(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    assert baseline is not None
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)

    git(repo, "reset", "--hard", baseline)
    state = load_lifecycle_state(repo)
    assert state is not None
    worker_result = WorkerResult.model_validate(batch_worker_payload())
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="worker",
        role="worker",
        turn=state.turn,
        session_id=state.sessions["worker"].session_id,
        result_kind="worker",
        result=worker_result.model_dump(mode="json"),
        transition_error="Product working tree is not clean: stale",
    )
    state.inflight = None
    state.next_session = "worker"
    save_lifecycle_state(repo, state)

    workers_before = worker_invocation_count(provider)
    outcome = run_lifecycle(repo, run_opts(2, resuming=True), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    assert worker_invocation_count(provider) == workers_before
    reloaded = load_lifecycle_state(repo)
    assert reloaded is not None
    repair = reloaded.inflight.repair_reason if reloaded.inflight else None
    assert repair is None or "handoff rejected" not in repair.lower()
