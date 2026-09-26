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
    make_repo,
    run_lifecycle,
    run_opts,
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
