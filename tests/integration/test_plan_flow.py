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
            "schema_version": 2,
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


def test_path_only_batch_does_not_advance_baseline(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore build")
    # Commit the gitignore as a separate approved... wait, that advances HEAD.
    # Approve that commit first via a git batch, or include gitignore in initial repo.
    # Simpler: put gitignore in the initial commit by writing before init? The repo already
    # has an empty init commit. Commit gitignore then approve it as a batch first.
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "W-ignore",
                "summary": "ignore generated output",
            },
            "work_summary": "gitignore",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_reviewer_pass("batch", "W-ignore")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    approved = load_lifecycle_state(repo).last_approved_commit
    build = repo / "build"
    build.mkdir()
    (build / "report.html").write_text("<html>ok</html>\n", encoding="utf-8")
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "generated-validation",
                "summary": "review generated report",
                "targets": [
                    {
                        "kind": "path",
                        "id": "generated",
                        "path": "build/report.html",
                        "purpose": "intentionally gitignored",
                    }
                ],
            },
            "work_summary": "generated report",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_response(
        "reviewer",
        {
            "schema_version": 2,
            "actor": "reviewer",
            "verdict": "pass",
            "scope": "batch",
            "target": "generated-validation",
            "reviewed_target_ids": ["generated"],
            "summary": "ok",
            "findings": [],
            "verification": [],
        },
    )
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == approved
    reviews = sorted((repo / ".auto-loop" / "reviews").glob("*.md"))
    assert any("generated" in path.read_text(encoding="utf-8") for path in reviews)


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
