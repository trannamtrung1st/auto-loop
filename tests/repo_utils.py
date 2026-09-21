"""Shared Git + workspace helpers for unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from auto_loop.init_cmd import bootstrap_workspace, run_init
from auto_loop.manifest import load_run_manifest
from auto_loop.config import AutoLoopConfig, load_frozen_config


def bootstrapped_manifest(repo: Path):
    return load_run_manifest(repo / ".ai" / "run.yaml")


def frozen_config(repo: Path) -> AutoLoopConfig:
    source = bootstrapped_manifest(repo)
    return load_frozen_config(source.artifact_root) or source.config


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "commit", "--allow-empty", "-m", "init")
    return repo


def initialized_repo(
    tmp_path: Path,
    *,
    name: str = "repo",
    minimal: bool = False,
    goal: str = "Test task",
) -> Path:
    repo = git_repo(tmp_path, name)
    bootstrap_workspace(repo, minimal=minimal, goal=goal)
    return repo


def user_init_repo(tmp_path: Path, *, name: str = "repo") -> Path:
    repo = git_repo(tmp_path, name)
    run_init(repo / ".ai" / "run.yaml")
    return repo
