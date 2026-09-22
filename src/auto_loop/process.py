"""Process-tree termination helpers.

The signal handler may call :func:`request_termination` only. That function
sends a signal and returns. Waiting, descendant cleanup, and identity checks
belong to :func:`terminate_process_tree`, which supervision and stale-runtime
reconciliation call outside the handler.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable

import psutil

# PID reuse can land on a new process within the same second. A sub-second
# window still rejects a recycled PID whose start time is not the one we stored.
CREATE_TIME_TOLERANCE_SECONDS = 0.5

ForceCheck = Callable[[], bool]


def process_create_time(pid: int) -> float | None:
    """Return the process start time, or None when the PID cannot be inspected."""
    if pid <= 0:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def process_matches(
    pid: int,
    create_time: float | None,
    *,
    tolerance: float = CREATE_TIME_TOLERANCE_SECONDS,
) -> bool:
    """True when ``pid`` is still the process that started at ``create_time``."""
    if create_time is None or pid <= 0:
        return False
    actual = process_create_time(pid)
    if actual is None:
        return False
    return abs(actual - float(create_time)) <= tolerance


def is_descendant(pid: int, ancestor_pid: int) -> bool:
    """True when ``pid`` is ``ancestor_pid`` or a live descendant of it."""
    if pid <= 0 or ancestor_pid <= 0:
        return False
    if pid == ancestor_pid:
        return True
    try:
        current = psutil.Process(pid)
        for parent in current.parents():
            if parent.pid == ancestor_pid:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    return False


def _separate_group(pid: int) -> int | None:
    """Process group to signal, excluding the caller's own group."""
    if os.name != "posix" or pid <= 0:
        return None
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return None
    if group == os.getpgrp():
        return None
    return group


def _signal_group_or_process(pid: int, sig: int) -> None:
    """Signal a provider process group, or the single PID when it shares ours."""
    if pid <= 0:
        return
    group = _separate_group(pid)
    if group is not None:
        try:
            os.killpg(group, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def request_termination(pid: int, *, force: bool = False) -> None:
    """Ask a process group to exit without waiting.

    Safe to call from a signal handler: no sleeps, no ``psutil`` walks, and no
    reaping. ``force`` sends ``SIGKILL``; otherwise ``SIGTERM``.
    """
    if pid <= 0:
        return
    _signal_group_or_process(pid, signal.SIGKILL if force else signal.SIGTERM)


def _pid_running(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return proc.is_running()
    except psutil.NoSuchProcess:
        return False


def _snapshot_tree_pids(root_pid: int) -> set[int]:
    """Capture the root and every descendant PID before signaling."""
    pids = {root_pid}
    try:
        root = psutil.Process(root_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return pids
    try:
        for proc in root.children(recursive=True):
            pids.add(proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return pids
    return pids


def _any_running(pids: set[int]) -> bool:
    return any(_pid_running(pid) for pid in pids)


def _signal_tree(pid: int, sig: int) -> None:
    """Signal the process group and any descendants that left it."""
    _signal_group_or_process(pid, sig)
    try:
        root = psutil.Process(pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for proc in descendants:
        try:
            proc.send_signal(sig)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    try:
        root.send_signal(sig)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return


def _kill_pids(pids: set[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def _wait_tree_dead(
    pids: set[int],
    seconds: float,
    force_check: ForceCheck | None,
) -> bool:
    if not _any_running(pids):
        return True
    if seconds <= 0:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if force_check is not None and force_check():
            return False
        if not _any_running(pids):
            return True
        time.sleep(0.05)
    return not _any_running(pids)


def terminate_process_tree(
    pid: int,
    *,
    graceful_seconds: float = 5.0,
    force: bool = False,
    expected_create_time: float | None = None,
    force_check: ForceCheck | None = None,
) -> bool:
    """Terminate a process group and descendants.

    Returns False when the PID is already gone or ``expected_create_time`` does
    not match the live process. A mismatched start time means the PID was
    reused; this function does not signal it.

    When ``force`` is false, members get ``SIGTERM`` and this waits up to
    ``graceful_seconds``. Survivors, and any stop that escalates via
    ``force_check``, then receive ``SIGKILL``.
    """
    if pid <= 0:
        return False
    if expected_create_time is not None and not process_matches(pid, expected_create_time):
        return False
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    if expected_create_time is not None:
        try:
            started = float(root.create_time())
        except psutil.NoSuchProcess:
            return False
        if abs(started - float(expected_create_time)) > CREATE_TIME_TOLERANCE_SECONDS:
            return False

    snapshot = _snapshot_tree_pids(pid)
    forced = force or (force_check is not None and force_check())
    if not forced:
        _signal_tree(pid, signal.SIGTERM)
        if _wait_tree_dead(snapshot, graceful_seconds, force_check):
            return True
    # The root can exit while a captured descendant keeps running, or the
    # kernel can reuse the root PID. Re-check root identity before SIGKILL.
    if expected_create_time is not None and not process_matches(pid, expected_create_time):
        survivors = {candidate for candidate in snapshot if _pid_running(candidate)}
        if survivors:
            _kill_pids(survivors, signal.SIGKILL)
            _wait_tree_dead(snapshot, 1.0, None)
        return not _any_running(snapshot)
    survivors = {candidate for candidate in snapshot if _pid_running(candidate)}
    if survivors:
        _signal_tree(pid, signal.SIGKILL)
        _kill_pids(survivors, signal.SIGKILL)
        _wait_tree_dead(snapshot, 1.0, None)
    return not _any_running(snapshot)
