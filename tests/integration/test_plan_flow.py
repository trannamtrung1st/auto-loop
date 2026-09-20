"""Fake-provider plan and batch lifecycle tests."""

import subprocess
from pathlib import Path

import pytest

from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.run_prerequisites import RunPreconditionError, ensure_run_prerequisites
from auto_loop.runtime import load_lifecycle_state


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    run_init(repo)
    return repo


def test_plan_review_pass_marks_plan_approved(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert list((repo / ".auto-loop" / "reviews").glob("*.md"))


def test_batch_pass_advances_baseline(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    base = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    head = head_commit(repo)
    provider.set_response(
        "worker",
        {
            "schema_version": 1,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "W01",
                "summary": "batch",
                "base_commit": base,
                "head_commit": head,
            },
            "work_summary": "implemented",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == head


def test_minimal_init_blocks_run(tmp_path: Path):
    repo = tmp_path / "minimal"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    run_init(repo, minimal=True)
    with pytest.raises(RunPreconditionError):
        ensure_run_prerequisites(repo)
