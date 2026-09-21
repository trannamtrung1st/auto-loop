"""Subprocess provider + controller session identity on truncated streams."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import create_lifecycle
from auto_loop.loop import LifecycleRunner
from auto_loop.providers.cursor import parse_cursor_stream
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.run_options import RunOptions
from auto_loop.runtime import save_lifecycle_state

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cursor_retry_cli.py"


def test_subprocess_provider_single_attempt_returns_partial_stream(tmp_path: Path, monkeypatch):
    """Inner provider must not retry; controller owns resume."""
    calls: list[list[str]] = []
    config = default_config()
    config.limits.provider_retries = 5
    provider = SubprocessCursorProvider(config)
    counter = tmp_path / "counter"
    monkeypatch.setenv("AUTO_LOOP_CURSOR_RETRY_COUNTER", str(counter))

    import auto_loop.providers.subprocess_cursor as sc_mod

    real_streaming = sc_mod.run_subprocess_streaming

    def counting_stream(argv, **kwargs):
        calls.append(list(argv))
        return real_streaming(argv, **kwargs)

    monkeypatch.setattr(sc_mod, "run_subprocess_streaming", counting_stream)
    argv = [sys.executable, str(FIXTURE), "-p", "prompt"]
    attempt = provider.invoke(argv)
    assert attempt.exit_code == 0
    assert len(calls) == 1
    parsed = parse_cursor_stream(attempt.lines)
    assert parsed.session_id == "retry-sess-fixed-001"
    assert not parsed.final_text


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_lifecycle_runner_retries_subprocess_with_resume(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    bootstrap_workspace(repo)
    counter = tmp_path / "counter"
    monkeypatch.setenv("AUTO_LOOP_CURSOR_RETRY_COUNTER", str(counter))

    config = default_config()
    config.limits.provider_retries = 2
    options = RunOptions(
        "auto",
        "auto",
        max_turns=5,
        max_runtime_minutes=60,
        verbose=False,
        quiet=True,
    )
    provider = SubprocessCursorProvider(config)
    runner = LifecycleRunner(repo, config, options, provider)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)

    import auto_loop.loop as loop_mod
    import auto_loop.providers.subprocess_cursor as sc_mod

    recorded: list[list[str]] = []
    real_streaming = sc_mod.run_subprocess_streaming

    def recording_stream(argv, **kwargs):
        recorded.append(list(argv))
        return real_streaming(argv, **kwargs)

    monkeypatch.setattr(sc_mod, "run_subprocess_streaming", recording_stream)

    def fake_build(_config, request, *, binary=None, resume_session_id=None):
        argv = [sys.executable, str(FIXTURE), "-p", "--workspace", str(repo)]
        if resume_session_id:
            argv.append(f"--resume={resume_session_id}")
        argv.append(request.prompt)
        return argv

    monkeypatch.setattr(loop_mod, "build_cursor_command", fake_build)

    text = runner._invoke_slot("planner", "plan the task", state)
    assert "AUTO_LOOP_RESULT" in text
    assert state.sessions["planner"].session_id == "retry-sess-fixed-001"
    assert len(recorded) == 2
    assert not any(arg.startswith("--resume=") for arg in recorded[0])
    assert "--resume=retry-sess-fixed-001" in recorded[1]
