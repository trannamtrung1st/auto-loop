"""Run observability: events and turn logs."""

import subprocess
from pathlib import Path

from auto_loop.config import load_config_from_repo
from auto_loop.events import load_events
from auto_loop.init_cmd import run_init
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state
from auto_loop.turn_logs import list_turn_numbers


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    run_init(repo)
    return repo


def test_plan_run_emits_events_and_turn_logs(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    config = load_config_from_repo(repo)
    events = load_events(repo, config)
    types = [event["type"] for event in events]
    assert "lifecycle_started" in types
    assert "review_requested" in types
    assert "review_result" in types
    state = load_lifecycle_state(repo)
    assert state is not None
    assert list_turn_numbers(repo, state.lifecycle_id)
