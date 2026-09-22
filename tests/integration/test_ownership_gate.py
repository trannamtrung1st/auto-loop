"""Run/resume must not start when ownership is remote or unverified."""

import json
from pathlib import Path

import pytest

from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import LifecycleStatus, create_lifecycle
from auto_loop.locking import ConcurrentRunError, lock_path
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.stop_control import active_run_path
from tests.repo_utils import frozen_config
from tests.integration.scenario_harness import make_repo


def _options() -> RunOptions:
    return RunOptions(
        "auto",
        "auto",
        max_turns=1,
        max_runtime_minutes=60,
        verbose=False,
        quiet=True,
    )


def test_run_lifecycle_refuses_remote_active_run_without_lock(tmp_path: Path):
    repo = make_repo(tmp_path)
    config = frozen_config(repo)
    artifact_root = repo / config.artifacts_root
    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.RUNNING
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    active_run_path(repo, artifact_root).write_text(
        json.dumps(
            {
                "controller_pid": 4242,
                "controller_hostname": "remote-host.example",
                "controller_started_at": 1.0,
                "lifecycle_id": state.lifecycle_id,
                "provider_pid": None,
                "provider_create_time": None,
            }
        ),
        encoding="utf-8",
    )
    assert not lock_path(repo, artifact_root).is_file()
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    with pytest.raises(ConcurrentRunError, match="another host"):
        run_lifecycle(
            repo,
            _options(),
            provider,
            config=config,
            artifact_root=artifact_root,
        )
    assert active_run_path(repo, artifact_root).is_file()
    reloaded = load_lifecycle_state(repo, artifact_root)
    assert reloaded is not None
    assert reloaded.status == LifecycleStatus.RUNNING


def test_run_lifecycle_refuses_unverified_active_run(tmp_path: Path):
    repo = make_repo(tmp_path)
    config = frozen_config(repo)
    artifact_root = repo / config.artifacts_root
    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.RUNNING
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    active_run_path(repo, artifact_root).write_text(
        json.dumps(
            {
                "controller_pid": 4242,
                "controller_started_at": 1.0,
                "lifecycle_id": state.lifecycle_id,
                "provider_pid": None,
                "provider_create_time": None,
            }
        ),
        encoding="utf-8",
    )
    provider = ScriptedProvider()
    with pytest.raises(ConcurrentRunError, match="unverified"):
        run_lifecycle(
            repo,
            _options(),
            provider,
            config=config,
            artifact_root=artifact_root,
        )


def test_stop_cli_remote_ownership_exits_concurrent_run(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    from auto_loop.cli import app

    repo = make_repo(tmp_path)
    yaml_path = repo / ".ai" / "run.yaml"
    config = frozen_config(repo)
    artifact_root = repo / config.artifacts_root
    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.RUNNING
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    active_run_path(repo, artifact_root).write_text(
        json.dumps(
            {
                "controller_pid": 4242,
                "controller_hostname": "remote-host.example",
                "controller_started_at": 1.0,
                "lifecycle_id": state.lifecycle_id,
                "provider_pid": None,
                "provider_create_time": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/bin/false")
    result = CliRunner().invoke(app, ["stop", str(yaml_path)])
    assert result.exit_code == int(ExitCode.CONCURRENT_RUN)
    assert "another host" in result.output.lower()
