"""False BLOCKED endgames route back to final review without widening COMPLETE."""

from __future__ import annotations

from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import CompletedProviderTurn, LifecycleStatus
from auto_loop.manifest import load_run_manifest
from auto_loop.models import FALSE_BLOCKER_ENDGAME_FINDING_ID, FALSE_BLOCKER_OPERATOR_REASON
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
    latest_review_text,
    make_repo,
    run_lifecycle,
    run_opts,
)


def _events(repo: Path) -> list[dict]:
    config = load_run_manifest(repo / ".ai" / "run.yaml").config
    return load_events(repo, config)


def _false_blocker_payload() -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "revise",
        "scope": "batch",
        "target": "blocked",
        "summary": FALSE_BLOCKER_OPERATOR_REASON,
        "findings": [
            {
                "id": FALSE_BLOCKER_ENDGAME_FINDING_ID,
                "title": "Request final review",
                "detail": "Frozen-task requirements are satisfied. Later milestones are outside the task.",
                "evidence": "task.md",
                "required_change": "Request scope=final.",
            }
        ],
        "verification": [],
    }


def _approve_batch(repo: Path, provider: PromptCapturingProvider) -> str:
    approve_plan(repo, provider)
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == head
    return head


def test_false_blocker_revise_then_final_complete(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approved = _approve_batch(repo, provider)
    before = len(provider.worker_prompts)
    provider.set_worker_blocked(
        "All task.md requirements are done. Phase 8 is outside this task."
    )
    provider.set_response("reviewer", _false_blocker_payload())
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.LIMIT_REACHED
    assert state.next_session == "worker"
    assert state.pending_revision is not None
    assert state.pending_revision.handoff_repair == "false_blocker_endgame"
    assert state.pending_revision.target == "blocked"
    review = latest_review_text(repo)
    assert "REVISE" in review
    assert FALSE_BLOCKER_OPERATOR_REASON in review
    assert FALSE_BLOCKER_ENDGAME_FINDING_ID in review
    assert any(event.get("type") == "blocked_handoff_rejected" for event in _events(repo))
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "status: limit_reached" in report
    assert "status: blocked" not in report
    assert "final handoff required" in report

    provider.set_worker_final_request(head=approved)
    provider.set_reviewer_complete(approved)
    completed = run_lifecycle(repo, run_opts(2), provider)
    assert completed.exit_code == ExitCode.COMPLETE
    assert load_completion_record(repo) is not None
    repair_prompts = [
        prompt
        for prompt in provider.worker_prompts[before:]
        if "rejected the BLOCKED handoff" in prompt
    ]
    assert repair_prompts
    assert "request scope=final" in repair_prompts[0]
    assert "Re-read the frozen task" in repair_prompts[0]
    final = load_lifecycle_state(repo)
    assert final is not None
    assert final.status == LifecycleStatus.COMPLETED
    assert final.pending_revision is None


def test_replayed_false_blocker_revise_survives_resume(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approved = _approve_batch(repo, provider)
    provider.set_worker_blocked("Nothing in task.md remains; the next milestone is out of scope.")
    run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.active_review is not None
    assert state.active_review.target == "blocked"
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="reviewer",
        role="reviewer",
        turn=state.turn,
        session_id=state.sessions["reviewer"].session_id,
        result_kind="reviewer",
        result=_false_blocker_payload(),
    )
    state.inflight = None
    save_lifecycle_state(repo, state)

    replayed = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert replayed.exit_code == ExitCode.LIMIT_REACHED
    restored = load_lifecycle_state(repo)
    assert restored is not None
    assert restored.pending_revision is not None
    assert restored.pending_revision.handoff_repair == "false_blocker_endgame"
    assert any(event.get("type") == "blocked_handoff_rejected" for event in _events(repo))

    before = len(provider.worker_prompts)
    provider.set_worker_final_request(head=approved)
    provider.set_reviewer_complete(approved)
    completed = run_lifecycle(repo, run_opts(2, resuming=True), provider)
    assert completed.exit_code == ExitCode.COMPLETE
    assert any(
        "rejected the BLOCKED handoff" in prompt for prompt in provider.worker_prompts[before:]
    )


def test_repeated_false_blocker_is_reviewed_again(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    summary = "All task.md requirements are done. Phase 8 is outside this task."
    reviewed_before = execution_reviewer_invocation_count(provider)
    provider.set_worker_blocked(summary)
    provider.set_response("reviewer", _false_blocker_payload())
    provider.set_worker_blocked(summary)
    provider.set_response("reviewer", _false_blocker_payload())
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert execution_reviewer_invocation_count(provider) == reviewed_before + 2
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status != LifecycleStatus.BLOCKED
    assert state.pending_revision is not None
    assert state.pending_revision.handoff_repair == "false_blocker_endgame"
    assert state.last_blocker_fingerprint is None


def test_genuine_blocker_pass_still_blocks(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_batch(repo, provider)
    provider.set_worker_blocked("Required deployment approval is missing")
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.BLOCKED
    assert not any(event.get("type") == "blocked_handoff_rejected" for event in _events(repo))


def test_local_work_revise_is_not_forced_finalization(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_batch(repo, provider)
    provider.set_worker_blocked("I think the task is done")
    provider.set_reviewer_revise("batch", "blocked", finding_id="local-work")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.LIMIT_REACHED
    assert state.pending_revision is not None
    assert state.pending_revision.handoff_repair is None
    assert "false_blocker_endgame" not in latest_review_text(repo)
    commit_file(repo, "fix.txt", "fix\n", "remaining in-scope fix")
    before = len(provider.worker_prompts)
    provider.set_response("worker", batch_worker_payload(target="W02"))
    provider.set_reviewer_pass("batch", "W02")
    continued = run_lifecycle(repo, run_opts(2), provider)
    assert continued.exit_code == ExitCode.LIMIT_REACHED
    assert all(
        "rejected the BLOCKED handoff" not in prompt for prompt in provider.worker_prompts[before:]
    )
    assert load_lifecycle_state(repo).status == LifecycleStatus.LIMIT_REACHED
    assert not (repo / ".ai/auto-loop/runtime/completion.json").is_file()


def test_plan_reviewer_cannot_use_endgame_repair_to_complete(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_planner_blocked("planning input is missing")
    provider.set_reviewer_revise(
        "plan",
        "blocked",
        finding_id=FALSE_BLOCKER_ENDGAME_FINDING_ID,
    )
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.phase == "planning"
    assert state.next_session == "planner"
    assert state.pending_revision is not None
    assert state.pending_revision.handoff_repair is None
    assert state.status != LifecycleStatus.COMPLETED


def test_unapproved_docs_commit_uses_batch_review_not_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_batch(repo, provider)
    docs_head = commit_file(repo, "docs.txt", "notes\n", "docs")
    provider.set_worker_blocked("Nothing else is in scope, but HEAD is ahead")
    provider.set_reviewer_revise("batch", "blocked", finding_id="review-docs-commit")
    provider.set_response("worker", batch_worker_payload(target="docs"))
    provider.set_reviewer_pass("batch", "docs")
    provider.set_worker_final_request(head=docs_head)
    provider.set_reviewer_complete(docs_head)
    outcome = run_lifecycle(repo, run_opts(6), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.COMPLETED
    assert state.last_approved_commit == docs_head
    assert head_commit(repo) == docs_head
    assert not any(event.get("type") == "blocked_handoff_rejected" for event in _events(repo))
    assert all("rejected the BLOCKED handoff" not in prompt for prompt in provider.worker_prompts)


def test_false_blocker_repair_does_not_skip_unapproved_head(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approved = _approve_batch(repo, provider)
    docs_head = commit_file(repo, "docs.txt", "notes\n", "docs")
    provider.set_worker_blocked("Task is done except this docs commit is unapproved")
    provider.set_response("reviewer", _false_blocker_payload())
    provider.set_worker_final_request(head=docs_head)
    provider.set_response("worker", batch_worker_payload(target="docs"))
    provider.set_reviewer_pass("batch", "docs")
    provider.set_worker_final_request(head=docs_head)
    provider.set_reviewer_complete(docs_head)
    outcome = run_lifecycle(repo, run_opts(8), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == docs_head
    assert approved != docs_head
    assert any(
        "Submit those changes for batch review" in prompt for prompt in provider.worker_prompts
    )
    assert any(event.get("type") == "blocked_handoff_rejected" for event in _events(repo))
