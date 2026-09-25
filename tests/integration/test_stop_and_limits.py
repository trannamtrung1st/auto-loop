"""Stop, protocol repair, and bounded limits."""

import subprocess
from pathlib import Path


from auto_loop.exits import ExitCode
from auto_loop.init_cmd import bootstrap_workspace
from tests.integration.scenario_harness import run_lifecycle
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
    bootstrap_workspace(repo)
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
    from tests.repo_utils import bootstrapped_manifest, frozen_config
    from auto_loop.loop import run_lifecycle as core_run_lifecycle

    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"run": cfg.run.model_copy(update={"protocol_retries": 0})})
    source = bootstrapped_manifest(repo)
    monkeypatch.setattr("auto_loop.loop.ensure_run_prerequisites", lambda _repo, _config: None)
    provider = ScriptedProvider()
    provider.set_invalid_protocol_response("planner")
    outcome = core_run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
        config=cfg,
        artifact_root=source.artifact_root,
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
    assert outcome.message is not None
    assert outcome.terminal_summary_rendered is False


def test_max_turns_normal_mode_marks_terminal_summary_rendered(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=False),
        provider,
    )
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert outcome.message is None
    assert outcome.terminal_summary_rendered is True


def test_limit_reached_resume_resets_status_to_running(tmp_path: Path, monkeypatch):
    from auto_loop.loop import LifecycleRunner
    from tests.integration.scenario_harness import (
        PromptCapturingProvider,
        batch_worker_payload,
        batch_worker_payload_with_path,
        commit_file,
        make_repo,
        run_lifecycle,
        run_opts,
    )
    from tests.integration.test_lifecycle_flows import _approve_plan

    recorded: list[LifecycleStatus] = []
    orig_save = LifecycleRunner._save_state

    def tracked_save(self, state):
        recorded.append(state.status)
        return orig_save(self, state)

    monkeypatch.setattr(LifecycleRunner, "_save_state", tracked_save)

    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    commit_file(repo, "feature.txt", "x\n", "feature")
    plan_rel = ".ai/auto-loop/plan.md"
    provider.set_response(
        "worker",
        batch_worker_payload_with_path(plan_rel, path_id="plan-bad"),
    )
    run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.LIMIT_REACHED
    recorded.clear()
    provider.set_response("worker", batch_worker_payload())
    run_lifecycle(repo, run_opts(1), provider)
    assert recorded
    assert recorded[0] == LifecycleStatus.RUNNING


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
