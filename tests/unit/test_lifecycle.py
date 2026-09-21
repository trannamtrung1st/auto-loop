"""Lifecycle state and prompt tests."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import (
    ActiveReview,
    LifecycleState,
    SessionRecord,
    create_lifecycle,
    migrate_lifecycle_data,
    next_cycle_id,
    session_consistency_errors,
)
from auto_loop.models import ActiveGitTarget
from auto_loop.prompts import TurnContext, build_reviewer_prompt, build_worker_prompt
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state


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
        task_path=".auto-loop/task.md",
        plan_path=".auto-loop/plan.md",
        latest_review_path=None,
        head_commit="abc123",
        product_clean=True,
        interrupted=True,
    )
    prompt = build_worker_prompt(state, ctx)
    assert ".auto-loop/task.md" in prompt
    assert "last approved product commit: abc123" in prompt
    assert "interrupted" in prompt.lower()
    assert "AUTO_LOOP_RESULT" in prompt


def test_reviewer_batch_and_final_prompts():
    state = create_lifecycle("base")
    state.plan_approved = True
    ctx = TurnContext(
        task_path=".auto-loop/task.md",
        plan_path=".auto-loop/plan.md",
        latest_review_path=".auto-loop/reviews/0001-plan.md",
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


def test_runtime_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
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
