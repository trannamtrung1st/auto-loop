"""Untrusted reviewer completions require fresh inspection after evidence is restored."""

from __future__ import annotations

import subprocess
from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import ActiveReview, CompletedProviderTurn, InflightMarker, create_lifecycle, utc_now
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.terminal_records import load_completion_record
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
from tests.repo_utils import frozen_config


def _prepare_batch_approved(repo: Path, provider) -> str:
    approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    assert run_lifecycle(repo, run_opts(2), provider).exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit
    return state.last_approved_commit


def _inject_failed_final_complete(
    repo: Path,
    *,
    candidate: str,
    reviewer_session: str,
    with_repair_inflight: bool = False,
) -> None:
    state = load_lifecycle_state(repo)
    assert state is not None
    from auto_loop.review_targets import sha256_file

    plan_path = repo / ".ai" / "auto-loop" / "plan.md"
    plan_hash = sha256_file(plan_path) if plan_path.is_file() else None
    state.active_review = ActiveReview(
        cycle_id="review-final-001",
        round=1,
        scope="final",
        target="whole-task",
        summary="final",
        session_purpose="reviewer",
        approved_base_commit=state.last_approved_commit,
        production_head_commit=candidate,
        current_candidate_head=candidate,
        plan_sha256=plan_hash,
    )
    state.next_session = "reviewer"
    state.sessions["reviewer"].session_id = reviewer_session
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="reviewer",
        role="reviewer",
        turn=state.turn,
        session_id=reviewer_session,
        result_kind="reviewer",
        result={
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "complete",
            "scope": "final",
            "target": "whole-task",
            "whole_task_reviewed": True,
            "summary": "STALE COMPLETE",
            "findings": [],
            "verification": [],
        },
        transition_error="COMPLETE requires HEAD to equal the active final candidate commit",
    )
    if with_repair_inflight:
        state.inflight = InflightMarker(
            session_slot="reviewer",
            role="reviewer",
            turn=state.turn,
            session_id=reviewer_session,
            started_at=utc_now(),
            repair_reason="Re-emit the result only",
            output_only_protocol_repair=True,
        )
    else:
        state.inflight = None
    save_lifecycle_state(repo, state)


def test_restored_head_discards_untrusted_complete_for_fresh_review(tmp_path: Path):
    repo = make_repo(tmp_path)
    capture = PromptCapturingProvider()
    approved = _prepare_batch_approved(repo, capture)
    reviewer_session = load_lifecycle_state(repo).sessions["reviewer"].session_id or "rev-1"
    capture.engine.sessions["reviewer"] = reviewer_session

    _inject_failed_final_complete(
        repo,
        candidate=approved,
        reviewer_session=reviewer_session,
        with_repair_inflight=True,
    )
    wrong_head = commit_file(repo, "ahead.txt", "y\n", "wrong")
    assert wrong_head != approved
    subprocess.run(["git", "reset", "--hard", approved], cwd=repo, check=True, capture_output=True)
    assert head_commit(repo) == approved

    stale_complete = (
        load_lifecycle_state(repo).completed_provider_turn.result.get("summary")
        if load_lifecycle_state(repo).completed_provider_turn
        else None
    )
    assert stale_complete == "STALE COMPLETE"

    capture.set_reviewer_complete()
    prompts_before = len(capture.reviewer_prompts)
    run_lifecycle(repo, run_opts(2, resuming=True), capture)

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is None
    assert state.inflight is None
    assert state.reviewer_fresh_inspection_required is False
    assert execution_reviewer_invocation_count(capture) >= 1
    reviewer_calls = [inv for inv in capture.engine.invocations if inv.role == "reviewer"]
    assert reviewer_calls[-1].resume_session_id == reviewer_session
    assert len(capture.reviewer_prompts) > prompts_before
    prompt = capture.reviewer_prompts[prompts_before]
    assert "discarded and is not trusted" in prompt
    assert "from scratch" in prompt
    assert "STALE COMPLETE" not in prompt
    assert load_completion_record(repo) is not None
    assert "reviewer_verdict_discarded_for_fresh_inspection" in [
        event.get("type") for event in load_events(repo, frozen_config(repo))
    ]


def test_untrusted_complete_not_replayed_when_evidence_still_wrong(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved = _prepare_batch_approved(repo, provider)
    reviewer_session = load_lifecycle_state(repo).sessions["reviewer"].session_id or "rev-1"
    _inject_failed_final_complete(repo, candidate=approved, reviewer_session=reviewer_session)
    commit_file(repo, "ahead.txt", "y\n", "still wrong")

    start = execution_reviewer_invocation_count(provider)
    run_lifecycle(repo, run_opts(0, resuming=True), provider)
    assert execution_reviewer_invocation_count(provider) == start
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "worker"
    assert state.active_review is None


def test_restored_plan_discards_untrusted_plan_reviewer_verdict(tmp_path: Path):
    repo = make_repo(tmp_path)
    capture = PromptCapturingProvider()
    from auto_loop.config import load_resolved_config_optional
    from auto_loop.review_targets import plan_path_target, sha256_file

    config = load_resolved_config_optional(repo / ".ai" / "auto-loop")
    assert config is not None
    plan_path = repo / config.plan_file
    original = plan_path.read_text(encoding="utf-8")
    plan_hash = sha256_file(plan_path)
    plan_target = plan_path_target(repo, config.plan_file, git_mode=config.git.mode)
    base = head_commit(repo)
    state = create_lifecycle(base)
    state.phase = "planning"
    state.next_session = "plan_reviewer"
    reviewer_session = "plan-reviewer-session"
    state.sessions["plan_reviewer"].session_id = reviewer_session
    capture.engine.sessions["plan_reviewer"] = reviewer_session
    state.active_review = ActiveReview(
        cycle_id="review-plan-001",
        round=1,
        scope="plan",
        target="plan",
        summary="plan review",
        session_purpose="plan_reviewer",
        plan_sha256=plan_hash,
        targets=[plan_target],
    )
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="plan_reviewer",
        role="reviewer",
        turn=state.turn,
        session_id=reviewer_session,
        result_kind="reviewer",
        result={
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "plan",
            "target": "plan",
            "summary": "STALE PASS",
            "findings": [],
            "verification": [],
            "reviewed_target_ids": ["plan"],
        },
        transition_error="path target plan changed during review",
    )
    save_lifecycle_state(repo, state)

    plan_path.write_text("# mutated plan\n", encoding="utf-8")
    plan_path.write_text(original, encoding="utf-8")

    capture.set_reviewer_pass("plan", "plan")
    prompts_before = len(capture.reviewer_prompts)
    run_lifecycle(repo, run_opts(1, resuming=True), capture)
    assert len(capture.reviewer_prompts) > prompts_before
    prompt = capture.reviewer_prompts[prompts_before]
    assert "from scratch" in prompt
    assert "STALE PASS" not in prompt
    reviewer_calls = [inv for inv in capture.engine.invocations if inv.role == "plan_reviewer"]
    assert reviewer_calls[-1].resume_session_id == reviewer_session
