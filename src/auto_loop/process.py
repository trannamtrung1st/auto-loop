"""Process-tree termination helpers."""

from __future__ import annotations

import os
import signal
import time
from typing import Iterable

import psutil


def _iter_tree(proc: psutil.Process) -> Iterable[psutil.Process]:
    children = proc.children(recursive=True)
    yield proc
    for child in children:
        yield child


def terminate_process_tree(
    pid: int,
    *,
    graceful_seconds: float = 5.0,
) -> None:
    """Gracefully terminate a process and descendants, then force-kill survivors."""
    if pid <= 0:
        return
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    for proc in _iter_tree(root):
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue

    deadline = time.monotonic() + graceful_seconds
    alive = True
    while time.monotonic() < deadline:
        try:
            alive = psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            alive = False
        if not alive:
            return
        time.sleep(0.05)

    for proc in _iter_tree(root):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
