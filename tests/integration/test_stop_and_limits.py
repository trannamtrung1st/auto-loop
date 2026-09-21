"""Stop, protocol repair, and bounded limits."""

import subprocess
from pathlib import Path

import pytest

from auto_loop.exits import ExitCode
from auto_loop.init_cmd import run_init
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state
from auto_loop.stop_control import request_remote_stop
from auto_loop.lifecycle import LifecycleStatus


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    run_init(repo)
    return repo


def test_protocol_repair_succeeds_then_plan_passes(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_invalid_protocol_response("planner")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.plan_approved is True
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_protocol_repair_exhaustion_returns_protocol_error(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    from auto_loop import config as config_mod

    cfg = config_mod.load_config_from_repo(repo)
    cfg.limits.protocol_retries = 0
    monkeypatch.setattr(
        "auto_loop.loop.ensure_run_prerequisites",
        lambda _repo: cfg,
    )
    provider = ScriptedProvider()
    provider.set_invalid_protocol_response("planner")
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.PROTOCOL_ERROR


def test_max_turns_sets_limit_reached_status(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert state.status == LifecycleStatus.LIMIT_REACHED


def test_stop_marks_running_lifecycle_stopped(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit
    from auto_loop.lifecycle import create_lifecycle
    from auto_loop.runtime import save_lifecycle_state

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    message = request_remote_stop(repo)
    assert "stopped" in message.lower()
    reloaded = load_lifecycle_state(repo)
    assert reloaded.status == LifecycleStatus.STOPPED
