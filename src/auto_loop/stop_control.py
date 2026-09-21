"""Graceful stop signaling and active run registration."""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from auto_loop.atomic_io import atomic_write_json
from auto_loop.exits import ExitCode
from auto_loop.lifecycle import LifecycleState, LifecycleStatus, utc_now
from auto_loop.locking import is_pid_alive, load_workspace_lock
from auto_loop.paths import auto_loop_root
from auto_loop.process import terminate_process_tree
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state


class StopError(Exception):
    exit_code = ExitCode.INTERNAL_ERROR


@dataclass
class ActiveRunRecord:
    controller_pid: int
    lifecycle_id: str
    provider_pid: int | None = None


def active_run_path(repo: Path) -> Path:
    return auto_loop_root(repo) / "runtime" / "active_run.json"


def register_active_run(repo: Path, lifecycle_id: str, provider_pid: int | None = None) -> None:
    record = ActiveRunRecord(
        controller_pid=os.getpid(),
        lifecycle_id=lifecycle_id,
        provider_pid=provider_pid,
    )
    atomic_write_json(
        active_run_path(repo),
        {
            "controller_pid": record.controller_pid,
            "lifecycle_id": record.lifecycle_id,
            "provider_pid": record.provider_pid,
        },
    )


def clear_active_run(repo: Path) -> None:
    path = active_run_path(repo)
    if path.is_file():
        path.unlink()


def load_active_run(repo: Path) -> ActiveRunRecord | None:
    path = active_run_path(repo)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return ActiveRunRecord(
        controller_pid=int(data["controller_pid"]),
        lifecycle_id=str(data["lifecycle_id"]),
        provider_pid=data.get("provider_pid"),
    )


def persist_stopped_state(repo: Path, state: LifecycleState | None) -> None:
    if state is None:
        return
    state.status = LifecycleStatus.STOPPED
    state.inflight = None
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state)


@dataclass
class RunStopController:
    """In-process stop flag wired to SIGINT/SIGTERM."""

    repo: Path
    requested: bool = False
    _previous_handlers: dict[int, object] = None  # type: ignore[assignment]

    def install(self) -> None:
        self._previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                continue

    def restore(self) -> None:
        if not self._previous_handlers:
            return
        for sig, handler in self._previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                continue

    def _handle(self, signum: int, _frame: object) -> None:
        self.requested = True


def request_remote_stop(repo: Path, *, wait_seconds: float = 2.0) -> str:
    lock = load_workspace_lock(repo)
    active = load_active_run(repo)
    target_pid = None
    if active is not None and is_pid_alive(active.controller_pid):
        target_pid = active.controller_pid
    elif lock is not None and is_pid_alive(lock.pid):
        target_pid = lock.pid
    if target_pid is None:
        state = load_lifecycle_state(repo)
        if state is not None and state.status == LifecycleStatus.RUNNING:
            persist_stopped_state(repo, state)
            return "No active controller process; lifecycle marked stopped."
        return "No active auto-loop controller found for this workspace."

    os.kill(target_pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = load_lifecycle_state(repo)
        if state is not None and state.status == LifecycleStatus.STOPPED:
            return "Stop signal delivered; lifecycle is stopped."
        time.sleep(0.05)

    if active is not None and active.provider_pid and is_pid_alive(active.provider_pid):
        terminate_process_tree(active.provider_pid)
    if is_pid_alive(target_pid):
        terminate_process_tree(target_pid)

    state = load_lifecycle_state(repo)
    if state is not None and state.status != LifecycleStatus.STOPPED:
        persist_stopped_state(repo, state)
    return "Stop signal delivered; lifecycle marked stopped."
