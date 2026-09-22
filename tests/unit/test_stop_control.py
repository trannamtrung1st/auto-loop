"""Stop controller behavior."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from auto_loop.process import process_create_time
from auto_loop.stop_control import FORCE_STOPPING_MESSAGE, STOPPING_MESSAGE, RunStopController


def _sleeper(*, ignore_term: bool, ready: Path | None = None) -> subprocess.Popen[bytes]:
    if ignore_term:
        script = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w', encoding='utf-8').write('ready')\n"
            "time.sleep(120)\n"
        )
        assert ready is not None
        argv = [sys.executable, "-c", script, str(ready)]
    else:
        argv = [sys.executable, "-c", "import time; time.sleep(120)"]
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_ready(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"process did not become ready: {path}")


def _reap(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        try:
            if proc is not None:
                proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    proc.wait(timeout=2)


def test_signal_handler_returns_without_waiting_for_graceful_timeout(
    monkeypatch, tmp_path: Path
):
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("signal handler must not wait in terminate_process_tree")

    monkeypatch.setattr("auto_loop.stop_control.terminate_process_tree", fail_if_called)
    ready = tmp_path / "ready"
    proc = _sleeper(ignore_term=True, ready=ready)
    _wait_ready(ready)
    try:
        ctrl = RunStopController(repo=Path("/tmp/repo"))
        ctrl.set_active_provider(proc.pid)
        started = time.monotonic()
        ctrl._handle(signal.SIGINT, None)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert ctrl.requested is True
        assert ctrl.force is False
        time.sleep(0.2)
        assert proc.poll() is None
    finally:
        _reap(proc)


def test_second_signal_force_kills_provider_that_ignores_sigterm(
    monkeypatch, tmp_path: Path
):
    written: list[bytes] = []

    def capture_write(_fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    monkeypatch.setattr("auto_loop.stop_control.os.write", capture_write)
    ready = tmp_path / "ready"
    proc = _sleeper(ignore_term=True, ready=ready)
    _wait_ready(ready)
    try:
        ctrl = RunStopController(repo=Path("/tmp/repo"))
        ctrl.set_active_provider(proc.pid)
        ctrl._handle(signal.SIGINT, None)
        time.sleep(0.2)
        assert proc.poll() is None
        started = time.monotonic()
        ctrl._handle(signal.SIGINT, None)
        proc.wait(timeout=2)
        assert time.monotonic() - started < 1.5
        assert proc.returncode is not None
        assert ctrl.force is True
    finally:
        _reap(proc)
    text = b"".join(written).decode()
    assert STOPPING_MESSAGE in text
    assert FORCE_STOPPING_MESSAGE in text


def test_handler_does_not_signal_without_a_provider(monkeypatch):
    calls: list[int] = []

    def record(pid: int, **_kwargs: object) -> None:
        calls.append(pid)

    monkeypatch.setattr("auto_loop.stop_control.request_termination", record)
    ctrl = RunStopController(repo=Path("/tmp/repo"))
    ctrl._handle(signal.SIGTERM, None)
    assert ctrl.requested is True
    assert calls == []


def test_register_active_run_stores_process_identity(tmp_path: Path):
    from auto_loop.stop_control import load_active_run, register_active_run

    repo = tmp_path / "repo"
    repo.mkdir()
    proc = _sleeper(ignore_term=False)
    try:
        register_active_run(repo, "lc-1", provider_pid=proc.pid)
        record = load_active_run(repo)
        assert record is not None
        assert record.controller_pid == os.getpid()
        assert record.provider_pid == proc.pid
        assert record.controller_started_at == pytest.approx(
            process_create_time(os.getpid()), abs=0.5
        )
        assert record.provider_create_time == pytest.approx(process_create_time(proc.pid), abs=0.5)
    finally:
        _reap(proc)
