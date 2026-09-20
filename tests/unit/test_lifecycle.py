"""Lifecycle state and prompt tests."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import (
    ActiveReview,
    LifecycleState,
    RoleSession,
    create_lifecycle,
    session_consistency_errors,
)
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
    assert state.next_actor == "worker"
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
        scope="batch",
        target="W01",
        summary="batch ready",
        base_commit="base",
        head_commit="head",
        worker_summary="implemented feature",
    )
    batch_prompt = build_reviewer_prompt(state, ctx, batch)
    assert "primary diff: base..head" in batch_prompt
    final_prompt = build_reviewer_prompt(
        state,
        ctx,
        ActiveReview(scope="final", target="whole-task", summary="final please"),
    )
    assert "whole-task final review" in final_prompt.lower()


def test_session_consistency_detects_equal_ids():
    now = datetime.now(timezone.utc)
    state = LifecycleState(
        lifecycle_id="id",
        initial_base_commit="a",
        last_approved_commit="a",
        sessions={
            "worker": RoleSession(session_id="same"),
            "reviewer": RoleSession(session_id="same"),
        },
        started_at=now,
        updated_at=now,
    )
    assert session_consistency_errors(state) != []


def test_runtime_round_trip(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    assert loaded.lifecycle_id == state.lifecycle_id
