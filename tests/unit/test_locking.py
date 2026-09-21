"""Workspace lock acquisition and stale takeover."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_loop.exits import ExitCode
from auto_loop.init_cmd import run_init
from auto_loop.locking import (
    ConcurrentRunError,
    acquire_workspace_lock,
    is_pid_alive,
    load_workspace_lock,
    lock_path,
)
def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)
    run_init(repo, minimal=True)
    return repo


def test_stale_lock_is_replaced(tmp_path: Path):
    repo = _git_repo(tmp_path)
    with patch("auto_loop.locking.is_pid_alive", return_value=False):
        first = acquire_workspace_lock(repo, "lc-a")
        first.release()
        second = acquire_workspace_lock(repo, "lc-b")
    record = load_workspace_lock(repo)
    assert record is not None
    assert record.lifecycle_id == "lc-b"
    second.release()
    assert not lock_path(repo).is_file()


def test_live_lock_raises_concurrent_run(tmp_path: Path):
    repo = _git_repo(tmp_path)
    with patch("auto_loop.locking.is_pid_alive", return_value=True):
        handle = acquire_workspace_lock(repo, "lc-live")
        with pytest.raises(ConcurrentRunError) as exc:
            acquire_workspace_lock(repo, "lc-other")
        assert exc.value.exit_code == ExitCode.CONCURRENT_RUN
        handle.release()


def test_is_pid_alive_false_for_missing_process():
    assert is_pid_alive(999_999_999) is False
