"""Lifecycle event log tests."""

import subprocess
from pathlib import Path

from auto_loop.config import load_config_from_repo
from auto_loop.events import append_event, load_events
from auto_loop.init_cmd import run_init


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    run_init(repo, minimal=True)
    return repo


def test_events_append_in_order(tmp_path: Path):
    repo = _repo(tmp_path)
    config = load_config_from_repo(repo)
    append_event(repo, config, {"type": "lifecycle_started", "lifecycle_id": "lc-1"})
    append_event(repo, config, {"type": "turn_started", "turn": 1, "actor": "worker"})
    events = load_events(repo, config)
    assert len(events) == 2
    assert events[0]["type"] == "lifecycle_started"
    assert events[0]["schema_version"] == 1
    assert events[1]["type"] == "turn_started"
    assert "ts" in events[0]
