"""Process-tree termination tests."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from auto_loop.process import (
    ProcessIdentity,
    _kill_identities,
    process_create_time,
    terminate_process_tree,
)


def _reap(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    proc.wait(timeout=2)


def _running(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE


def test_terminate_process_tree_stops_child_sleep():
    script = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
time.sleep(120)
"""
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL)
    time.sleep(0.3)
    try:
        terminate_process_tree(proc.pid, graceful_seconds=1.0)
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        _reap(proc)


def test_terminate_process_group_stops_grandchild(tmp_path: Path):
    grandchild_path = tmp_path / "grandchild.pid"
    script = """
import pathlib, subprocess, sys, time
path, exe = sys.argv[1], sys.executable
child = subprocess.Popen([exe, "-c", "import time; time.sleep(120)"])
pathlib.Path(path).write_text(str(child.pid), encoding="utf-8")
time.sleep(120)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(grandchild_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        grandchild_pid = 0
        while time.monotonic() < deadline:
            if grandchild_path.is_file():
                grandchild_pid = int(grandchild_path.read_text(encoding="utf-8"))
                if _running(grandchild_pid):
                    break
            time.sleep(0.05)
        assert _running(grandchild_pid)
        assert os.getpgid(proc.pid) == proc.pid
        terminate_process_tree(proc.pid, graceful_seconds=1.0)
        proc.wait(timeout=5)
        assert not _running(proc.pid)
        assert not _running(grandchild_pid)
    finally:
        _reap(proc)


def test_terminate_kills_child_that_ignores_sigterm_after_parent_exits(tmp_path: Path):
    child_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time\n"
        "path, exe = sys.argv[1], sys.executable\n"
        "child = subprocess.Popen(\n"
        "    [exe, '-c', 'import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "pathlib.Path(path).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(child_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        child_pid = 0
        while time.monotonic() < deadline:
            if child_path.is_file():
                child_pid = int(child_path.read_text(encoding="utf-8"))
                if _running(child_pid):
                    break
            time.sleep(0.05)
        assert _running(child_pid)
        terminate_process_tree(proc.pid, graceful_seconds=0.5)
        proc.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _running(child_pid):
            time.sleep(0.05)
        assert not _running(child_pid)
    finally:
        _reap(proc)


def test_kill_identities_skips_recycled_pid(monkeypatch):
    killed: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append(pid)

    def fake_match(pid: int, create_time: float | None, **_kwargs: object) -> bool:
        return pid == 42 and create_time == 10.0

    monkeypatch.setattr("auto_loop.process.os.kill", fake_kill)
    monkeypatch.setattr("auto_loop.process.process_matches", fake_match)
    identity = ProcessIdentity(42, 10.0)
    wrong = ProcessIdentity(42, 99.0)
    _kill_identities({identity, wrong}, signal.SIGKILL)
    assert killed == [42]


def test_terminate_skips_reused_pid(tmp_path: Path):
    del tmp_path
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        real_started = process_create_time(proc.pid)
        assert real_started is not None
        killed = terminate_process_tree(
            proc.pid,
            graceful_seconds=0.2,
            expected_create_time=real_started - 50,
        )
        assert killed is False
        time.sleep(0.1)
        assert proc.poll() is None
    finally:
        _reap(proc)
