"""Lifecycle state and prompt tests."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import (
    ActiveReview,
    LifecycleState,
    SessionRecord,
    adopt_session_identity,
    create_lifecycle,
    migrate_lifecycle_data,
    next_cycle_id,
    session_consistency_errors,
)
from auto_loop.providers.cursor import SessionError
from auto_loop.models import ActiveGitTarget, ActivePathTarget
from auto_loop.prompts import TurnContext, build_reviewer_prompt, build_worker_prompt
from auto_loop.review_targets import fingerprint_path
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state, state_path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    return repo


def test_create_lifecycle_sets_baselines_to_head(tmp_path: Path):
    repo = _repo(tmp_path)
    head = head_commit(repo)
    state = create_lifecycle(head)
    assert state.initial_base_commit == head
    assert state.last_approved_commit == head
    assert state.next_session == "planner"
    assert state.phase == "planning"
    assert state.plan_approved is False
    assert state.turn == 1


def test_worker_prompt_includes_durable_facts():
    state = create_lifecycle("abc123")
    ctx = TurnContext(
        task_path=".ai/auto-loop/task.md",
        plan_path=".ai/auto-loop/plan.md",
        latest_review_path=None,
        head_commit="abc123",
        product_clean=True,
        interrupted=True,
    )
    prompt = build_worker_prompt(state, ctx)
    assert ".ai/auto-loop/task.md" in prompt
    assert "last approved product commit: abc123" in prompt
    assert "interrupted" in prompt.lower()
    assert "AUTO_LOOP_RESULT" in prompt


def test_worker_prompt_surfaces_controller_repair_reason():
    state = create_lifecycle("abc123")
    ctx = TurnContext(
        task_path=".ai/auto-loop/task.md",
        plan_path=".ai/auto-loop/plan.md",
        latest_review_path=None,
        head_commit="abc123",
        product_clean=True,
        repair_reason="Final review requires explicit path, content, or Git targets",
    )
    prompt = build_worker_prompt(state, ctx)
    assert "controller rejected it" in prompt.lower()
    assert "Final review requires explicit path" in prompt
    assert "interrupted" not in prompt.lower()


def test_reviewer_batch_and_final_prompts():
    state = create_lifecycle("base")
    state.plan_approved = True
    ctx = TurnContext(
        task_path=".ai/auto-loop/task.md",
        plan_path=".ai/auto-loop/plan.md",
        latest_review_path=".ai/auto-loop/reviews/0001-plan.md",
        head_commit="head",
        product_clean=True,
    )
    batch = ActiveReview(
        cycle_id="review-0001",
        scope="batch",
        target="W01",
        summary="batch ready",
        approved_base_commit="base",
        current_candidate_head="head",
        worker_summary="implemented feature",
        targets=[ActiveGitTarget(base_commit="base", head_commit="head")],
    )
    batch_prompt = build_reviewer_prompt(state, ctx, batch)
    assert "primary diff: base..head" in batch_prompt
    final_prompt = build_reviewer_prompt(
        state,
        ctx,
        ActiveReview(cycle_id="review-0002", scope="final", target="whole-task", summary="final please"),
    )
    assert "whole-task final review" in final_prompt.lower()


def test_final_reviewer_prompt_lists_required_targets():
    state = create_lifecycle("base")
    state.plan_approved = True
    ctx = TurnContext(
        task_path=".ai/auto-loop/task.md",
        plan_path=".ai/auto-loop/plan.md",
        latest_review_path=None,
        head_commit="head",
        product_clean=True,
    )
    final = ActiveReview(
        cycle_id="review-0002",
        scope="final",
        target="whole-task",
        summary="final please",
        targets=[
            ActivePathTarget(
                id="report",
                path="report.md",
                fingerprint="deadbeef",
                exists=True,
                git_classification="untracked",
            )
        ],
    )
    prompt = build_reviewer_prompt(state, ctx, final)
    assert "Required review targets:" in prompt
    assert "- report: path `report.md`" in prompt
    assert "reviewed_target_ids" in prompt


def test_reviewer_prompt_surfaces_controller_repair_reason():
    state = create_lifecycle("base")
    state.plan_approved = True
    reason = "Approval requires reviewed_target_ids to include every active review target"
    ctx = TurnContext(
        task_path=".ai/auto-loop/task.md",
        plan_path=".ai/auto-loop/plan.md",
        latest_review_path=None,
        head_commit="head",
        product_clean=True,
        repair_reason=reason,
    )
    batch = ActiveReview(
        cycle_id="review-0001",
        scope="batch",
        target="W01",
        summary="batch ready",
        worker_summary="done",
        targets=[],
    )
    batch_prompt = build_reviewer_prompt(state, ctx, batch)
    assert "controller rejected it" in batch_prompt.lower()
    assert reason in batch_prompt

    plan_review = ActiveReview(
        cycle_id="review-plan",
        scope="plan",
        target="plan",
        summary="plan ready",
        session_purpose="plan_reviewer",
        round=2,
    )
    plan_prompt = build_reviewer_prompt(state, ctx, plan_review)
    assert "review cycle: review-plan" in plan_prompt
    assert "- scope: plan" in plan_prompt
    assert "- target: plan" in plan_prompt
    assert "echo `scope` and `target` exactly" in plan_prompt
    assert "controller rejected it" in plan_prompt.lower()
    assert reason in plan_prompt


def test_session_consistency_detects_equal_ids():
    now = datetime.now(timezone.utc)
    state = LifecycleState(
        lifecycle_id="id",
        initial_base_commit="a",
        last_approved_commit="a",
        sessions={
            "planner": SessionRecord(role="planner"),
            "plan_reviewer": SessionRecord(role="reviewer"),
            "worker": SessionRecord(role="worker", session_id="same"),
            "reviewer": SessionRecord(role="reviewer", session_id="same"),
        },
        started_at=now,
        updated_at=now,
    )
    assert session_consistency_errors(state) != []


def test_four_session_ids_must_be_unique():
    now = datetime.now(timezone.utc)
    state = LifecycleState(
        lifecycle_id="id",
        initial_base_commit="a",
        last_approved_commit="a",
        phase="execution",
        next_session="worker",
        plan_approved=True,
        sessions={
            "planner": SessionRecord(role="planner", session_id="p1"),
            "plan_reviewer": SessionRecord(role="reviewer", session_id="pr1"),
            "worker": SessionRecord(role="worker", session_id="w1"),
            "reviewer": SessionRecord(role="reviewer", session_id="r1"),
        },
        started_at=now,
        updated_at=now,
    )
    assert session_consistency_errors(state) == []
    state.sessions["reviewer"].session_id = "w1"
    assert session_consistency_errors(state) != []


def test_phase_next_session_validation():
    now = datetime.now(timezone.utc)
    state = LifecycleState(
        lifecycle_id="id",
        initial_base_commit="a",
        last_approved_commit="a",
        phase="planning",
        next_session="worker",
        sessions={
            "planner": SessionRecord(role="planner"),
            "plan_reviewer": SessionRecord(role="reviewer"),
            "worker": SessionRecord(role="worker"),
            "reviewer": SessionRecord(role="reviewer"),
        },
        started_at=now,
        updated_at=now,
    )
    assert any("planning phase" in err for err in session_consistency_errors(state))
    state.phase = "execution"
    state.next_session = "planner"
    assert any("execution phase" in err for err in session_consistency_errors(state))


def test_v2_state_without_history_reconciliation_field_loads():
    data = create_lifecycle("abc").model_dump(mode="json")
    data.pop("history_reconciliation")
    state = LifecycleState.model_validate(migrate_lifecycle_data(data))
    assert state.history_reconciliation is None


def test_runtime_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    assert loaded.lifecycle_id == state.lifecycle_id


def test_review_cycle_ids_increment():
    state = create_lifecycle("abc")
    first = next_cycle_id(state)
    second = next_cycle_id(state)
    assert first == "review-0001"
    assert second == "review-0002"
    assert state.review_cycle_seq == 2


def test_migrate_v1_unapproved_starts_fresh_planning():
    data = {
        "schema_version": 1,
        "lifecycle_id": "old",
        "status": "running",
        "turn": 2,
        "next_actor": "reviewer",
        "plan_approved": False,
        "initial_base_commit": "aaa",
        "last_approved_commit": "aaa",
        "sessions": {
            "worker": {"session_id": "w1", "model": "auto"},
            "reviewer": {"session_id": "r1", "model": "auto"},
        },
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    migrated = migrate_lifecycle_data(data)
    state = LifecycleState.model_validate(migrated)
    assert state.phase == "planning"
    assert state.next_session == "planner"
    assert state.sessions["planner"].session_id is None
    assert state.legacy_v1_sessions is not None


def test_migrate_v1_approved_keeps_execution_sessions():
    data = {
        "schema_version": 1,
        "lifecycle_id": "old",
        "status": "running",
        "turn": 4,
        "next_actor": "worker",
        "plan_approved": True,
        "initial_base_commit": "aaa",
        "last_approved_commit": "bbb",
        "sessions": {
            "worker": {"session_id": "w1", "model": "gpt"},
            "reviewer": {"session_id": "r1", "model": "claude"},
        },
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    migrated = migrate_lifecycle_data(data)
    state = LifecycleState.model_validate(migrated)
    assert state.phase == "execution"
    assert state.next_session == "worker"
    assert state.sessions["worker"].session_id == "w1"
    assert state.sessions["planner"].status == "legacy_not_created"


def test_adopt_session_identity_first_assign_and_mismatch():
    state = create_lifecycle("abc")
    assert adopt_session_identity(state, "planner", "sess-a", "auto") is True
    assert state.sessions["planner"].session_id == "sess-a"
    assert adopt_session_identity(state, "planner", "sess-a", "auto") is False
    with pytest.raises(SessionError, match="observed new id"):
        adopt_session_identity(state, "planner", "sess-b", "auto")


def test_adopt_session_identity_rejects_duplicate_across_slots():
    state = create_lifecycle("abc")
    adopt_session_identity(state, "worker", "shared", "auto")
    with pytest.raises(SessionError, match="duplicate session id"):
        adopt_session_identity(state, "reviewer", "shared", "auto")


def test_migrate_v1_active_batch_review_while_waiting_on_reviewer():
    data = {
        "schema_version": 1,
        "lifecycle_id": "old",
        "status": "running",
        "turn": 5,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": "aaa",
        "last_approved_commit": "aaa",
        "sessions": {
            "worker": {"session_id": "w1", "model": "auto"},
            "reviewer": {"session_id": "r1", "model": "auto"},
        },
        "active_review": {
            "scope": "batch",
            "target": "W01",
            "summary": "batch",
            "base_commit": "aaa",
            "head_commit": "bbb",
        },
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    migrated = migrate_lifecycle_data(data)
    state = LifecycleState.model_validate(migrated)
    assert state.next_session == "reviewer"
    assert state.active_review is not None
    assert state.active_review.cycle_id.startswith("review-")
    assert state.active_review.scope == "batch"
    assert state.active_review.session_purpose == "reviewer"
    assert state.active_review.has_git_target
    assert state.active_review.git_base == "aaa"
    assert state.active_review.git_head == "bbb"
    assert state.active_review.approved_base_commit == "aaa"
    assert state.active_review.current_candidate_head == "bbb"


def test_load_lifecycle_enriches_migrated_plan_target_fingerprint(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    plan_path = repo / ".ai/auto-loop" / "plan.md"
    digest, _ = fingerprint_path(plan_path)
    from auto_loop.git import head_commit

    head = head_commit(repo)
    v1 = {
        "schema_version": 1,
        "lifecycle_id": "x",
        "status": "running",
        "turn": 1,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": head,
        "last_approved_commit": head,
        "sessions": {
            "worker": {"session_id": "w", "model": "auto"},
            "reviewer": {"session_id": "r", "model": "auto"},
        },
        "active_review": {"scope": "plan", "target": "plan", "summary": "s"},
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    import json

    state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    state_path(repo).write_text(json.dumps(v1), encoding="utf-8")
    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    plan = next(t for t in loaded.active_review.targets if t.id == "plan")
    assert plan.path == ".ai/auto-loop/plan.md"
    assert plan.fingerprint == digest


def test_migrate_v1_execution_plan_review_uses_reviewer_slot():
    data = {
        "schema_version": 1,
        "lifecycle_id": "old",
        "status": "running",
        "turn": 6,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": "aaa",
        "last_approved_commit": "bbb",
        "sessions": {
            "worker": {"session_id": "w1", "model": "auto"},
            "reviewer": {"session_id": "r1", "model": "auto"},
        },
        "active_review": {
            "scope": "plan",
            "target": "plan-update",
            "summary": "worker amended plan",
        },
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    migrated = migrate_lifecycle_data(data)
    state = LifecycleState.model_validate(migrated)
    assert state.active_review is not None
    assert state.active_review.session_purpose == "reviewer"
    assert state.active_review.scope == "plan"
    assert any(t.id == "plan" for t in state.active_review.targets)


def test_migrate_v1_unmigratable_active_review_routes_to_worker():
    data = {
        "schema_version": 1,
        "lifecycle_id": "old",
        "status": "running",
        "turn": 5,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": "aaa",
        "last_approved_commit": "aaa",
        "sessions": {
            "worker": {"session_id": "w1", "model": "auto"},
            "reviewer": {"session_id": "r1", "model": "auto"},
        },
        "active_review": {"scope": "batch", "target": ""},
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    migrated = migrate_lifecycle_data(data)
    state = LifecycleState.model_validate(migrated)
    assert state.active_review is None
    assert state.next_session == "worker"
