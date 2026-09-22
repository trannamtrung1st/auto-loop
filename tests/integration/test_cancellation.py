"""Cancellation ownership: signals, orphan providers, and resume."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

from auto_loop.atomic_io import atomic_write_json
from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import InflightMarker, LifecycleStatus, create_lifecycle, utc_now
from auto_loop.locking import WorkspaceLockRecord, lock_path
from auto_loop.loop import run_lifecycle
from auto_loop.process import process_create_time
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.providers.supervision import (
    ProviderAttemptResult,
    provider_attempt_from_outcome,
    run_subprocess_streaming,
)
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.stop_control import active_run_path, request_remote_stop
from tests.integration.scenario_harness import make_repo
from tests.repo_utils import bootstrapped_manifest

_STREAM_SCRIPT = """
import json, subprocess, sys, time
grandchild_path = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(grandchild_path, "w", encoding="utf-8").write(str(child.pid))
print(json.dumps({"type": "thinking", "text": "partial"}), flush=True)
time.sleep(120)
"""


def _running(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE


def _reap(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    proc.wait(timeout=2)


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _options(*, resuming: bool = False) -> RunOptions:
    return RunOptions(
        "auto",
        "auto",
        max_turns=1,
        max_runtime_minutes=60,
        verbose=False,
        quiet=True,
        resuming=resuming,
    )


class _StreamingProvider:
    uses_live_cursor = False
    stop_check = None
    force_check = None
    on_provider_pid = None
    on_stream_line = None
    invokes = 0

    def __init__(self, grandchild_path: Path) -> None:
        self.grandchild_path = grandchild_path

    def prepare(self, role: str) -> None:
        del role

    def invoke(self, argv: list[str]) -> ProviderAttemptResult:
        del argv
        self.invokes += 1
        outcome = run_subprocess_streaming(
            [sys.executable, "-c", _STREAM_SCRIPT, str(self.grandchild_path)],
            wall_timeout_seconds=20.0,
            idle_timeout_seconds=20.0,
            stop_check=self.stop_check,
            force_check=self.force_check,
            on_provider_pid=self.on_provider_pid,
            on_line=self.on_stream_line,
            poll_interval=0.05,
            graceful_seconds=1.0,
        )
        return provider_attempt_from_outcome(outcome)


def _plant_orphan(
    repo: Path,
    artifact_root: Path,
    provider: subprocess.Popen[bytes],
    *,
    provider_create_time: float | None,
    controller_pid: int = 999_999_999,
    controller_started_at: float = 1.0,
    lock_pid: int | None = None,
    lock_create_time: float | None = 1.0,
) -> None:
    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.RUNNING
    state.next_session = "worker"
    state.inflight = InflightMarker(
        session_slot="planner",
        role="planner",
        turn=1,
        session_id="pending",
        started_at=utc_now(),
        head_before=head_commit(repo),
    )
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    atomic_write_json(
        active_run_path(repo, artifact_root),
        {
            "controller_pid": controller_pid,
            "controller_started_at": controller_started_at,
            "lifecycle_id": state.lifecycle_id,
            "provider_pid": provider.pid,
            "provider_create_time": provider_create_time,
        },
    )
    lock_pid = provider.pid if lock_pid is None else lock_pid
    record = WorkspaceLockRecord(
        pid=lock_pid,
        hostname=socket.gethostname(),
        started_at=datetime.now().astimezone(),
        lifecycle_id=state.lifecycle_id,
        process_create_time=lock_create_time,
    )
    atomic_write_json(lock_path(repo, artifact_root), record.model_dump(mode="json"))


def test_ctrl_c_stops_provider_tree_and_resume_continues(tmp_path: Path):
    repo = make_repo(tmp_path)
    source = bootstrapped_manifest(repo)
    grandchild_path = tmp_path / "grandchild.pid"
    provider = _StreamingProvider(grandchild_path)
    errors: list[str] = []

    def _signal_after_partial_line() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            logs = list(repo.glob("**/*.jsonl"))
            if any("partial" in path.read_text(encoding="utf-8") for path in logs if path.is_file()):
                time.sleep(0.05)
                os.kill(os.getpid(), signal.SIGINT)
                return
            time.sleep(0.05)
        errors.append("partial provider line was not persisted")

    watcher = threading.Thread(target=_signal_after_partial_line, daemon=True)
    watcher.start()
    try:
        outcome = run_lifecycle(
            repo,
            _options(),
            provider,
            config=source.config,
            artifact_root=source.artifact_root,
        )
    finally:
        watcher.join(timeout=2)
    assert errors == []
    assert outcome.exit_code == ExitCode.STOPPED
    assert provider.invokes == 1
    assert not active_run_path(repo, source.artifact_root).is_file()
    assert not lock_path(repo, source.artifact_root).is_file()
    stopped = load_lifecycle_state(repo, source.artifact_root)
    assert stopped is not None
    assert stopped.status == LifecycleStatus.STOPPED
    assert stopped.inflight is not None
    assert stopped.inflight.session_slot == "planner"
    events = load_events(repo, source.config)
    assert any(event.get("type") == "lifecycle_stopped" for event in events)
    assert not any(event.get("type") == "provider_retry" for event in events)
    assert grandchild_path.is_file()
    assert not _running(int(grandchild_path.read_text(encoding="utf-8")))
    jsonl_files = [path for path in source.artifact_root.glob("**/*.jsonl") if path.is_file()]
    log_files = [path for path in source.artifact_root.glob("**/*.log") if path.is_file()]
    assert any("partial" in path.read_text(encoding="utf-8") for path in jsonl_files + log_files)
    for path in jsonl_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)

    follow = ScriptedProvider()
    follow.set_planner_review_request()
    resumed = run_lifecycle(
        repo,
        _options(resuming=True),
        follow,
        config=source.config,
        artifact_root=source.artifact_root,
    )
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    assert any(event.get("type") == "inflight_resume" for event in load_events(repo, source.config))


def test_stop_kills_orphan_provider_of_dead_controller(tmp_path: Path):
    repo = make_repo(tmp_path)
    source = bootstrapped_manifest(repo)
    orphan = _sleeper()
    try:
        started = process_create_time(orphan.pid)
        assert started is not None
        _plant_orphan(repo, source.artifact_root, orphan, provider_create_time=started)
        message = request_remote_stop(repo, artifact_root=source.artifact_root)
        assert "stopped" in message.lower()
        orphan.wait(timeout=3)
        assert not _running(orphan.pid)
        assert not active_run_path(repo, source.artifact_root).is_file()
        assert not lock_path(repo, source.artifact_root).is_file()
        state = load_lifecycle_state(repo, source.artifact_root)
        assert state is not None
        assert state.status == LifecycleStatus.STOPPED
        assert state.inflight is not None
        assert state.inflight.session_slot == "planner"
        again = request_remote_stop(repo, artifact_root=source.artifact_root)
        assert "already stopped" in again.lower()
        reloaded = load_lifecycle_state(repo, source.artifact_root)
        assert reloaded is not None
        assert reloaded.status == LifecycleStatus.STOPPED
        assert reloaded.inflight is not None
    finally:
        _reap(orphan)


def test_resume_kills_orphan_and_ignores_reused_lock_pid(tmp_path: Path):
    repo = make_repo(tmp_path)
    source = bootstrapped_manifest(repo)
    orphan = _sleeper()
    try:
        started = process_create_time(orphan.pid)
        assert started is not None
        _plant_orphan(
            repo,
            source.artifact_root,
            orphan,
            provider_create_time=started,
            lock_pid=os.getpid(),
            lock_create_time=1.0,
        )
        follow = ScriptedProvider()
        follow.set_planner_review_request()
        outcome = run_lifecycle(
            repo,
            _options(resuming=True),
            follow,
            config=source.config,
            artifact_root=source.artifact_root,
        )
        assert outcome.exit_code == ExitCode.LIMIT_REACHED
        orphan.wait(timeout=3)
        assert not _running(orphan.pid)
        assert not lock_path(repo, source.artifact_root).is_file()
        events = load_events(repo, source.config)
        assert any(event.get("type") == "inflight_resume" for event in events)
    finally:
        _reap(orphan)


def test_stop_does_not_kill_reused_provider_pid(tmp_path: Path):
    repo = make_repo(tmp_path)
    source = bootstrapped_manifest(repo)
    decoy = _sleeper()
    try:
        _plant_orphan(
            repo,
            source.artifact_root,
            decoy,
            provider_create_time=1.0,
            controller_pid=decoy.pid,
            controller_started_at=1.0,
            lock_pid=decoy.pid,
            lock_create_time=1.0,
        )
        message = request_remote_stop(repo, artifact_root=source.artifact_root)
        assert "stopped" in message.lower()
        time.sleep(0.2)
        assert decoy.poll() is None
        state = load_lifecycle_state(repo, source.artifact_root)
        assert state is not None
        assert state.status == LifecycleStatus.STOPPED
    finally:
        _reap(decoy)
