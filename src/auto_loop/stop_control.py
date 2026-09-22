"""Stop signaling, active-run identity, and stale runtime reconciliation."""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from auto_loop.atomic_io import atomic_write_json
from auto_loop.exits import ExitCode
from auto_loop.lifecycle import LifecycleState, LifecycleStatus, utc_now
from auto_loop.locking import (
    LockError,
    is_pid_alive,
    load_workspace_lock,
    lock_blocks_workspace,
    lock_owner_verified_local,
    lock_path,
)
from auto_loop.paths import auto_loop_root
from auto_loop.process import (
    is_descendant,
    process_create_time,
    process_matches,
    request_termination,
    terminate_process_tree,
)
from auto_loop.runtime import RuntimeStateError, load_lifecycle_state, save_lifecycle_state

STOPPING_MESSAGE = "Stopping active agent…"
FORCE_STOPPING_MESSAGE = "Force stopping…"


class StopError(Exception):
    exit_code = ExitCode.INTERNAL_ERROR


@dataclass
class ActiveRunRecord:
    controller_pid: int
    lifecycle_id: str
    provider_pid: int | None = None
    controller_hostname: str | None = None
    controller_started_at: float | None = None
    provider_create_time: float | None = None


@dataclass
class RuntimeReconcileResult:
    """What :func:`reconcile_stale_runtime` changed."""

    controller_alive: bool = False
    provider_terminated: bool = False
    active_run_cleared: bool = False
    lock_cleared: bool = False
    state_marked_stopped: bool = False
    unverified_provider_skipped: bool = False
    remote_ownership: bool = False
    unverified_ownership: bool = False
    message: str = ""

    @property
    def changed(self) -> bool:
        return any(
            (
                self.provider_terminated,
                self.active_run_cleared,
                self.lock_cleared,
                self.state_marked_stopped,
            )
        )


def active_run_path(repo: Path, artifact_root: Path | None = None) -> Path:
    root = artifact_root if artifact_root is not None else auto_loop_root(repo)
    return root / "runtime" / "active_run.json"


def register_active_run(
    repo: Path,
    lifecycle_id: str,
    provider_pid: int | None = None,
    artifact_root: Path | None = None,
) -> None:
    controller_pid = os.getpid()
    provider_create_time = process_create_time(provider_pid) if provider_pid else None
    atomic_write_json(
        active_run_path(repo, artifact_root),
        {
            "controller_pid": controller_pid,
            "controller_hostname": socket.gethostname(),
            "controller_started_at": process_create_time(controller_pid),
            "lifecycle_id": lifecycle_id,
            "provider_pid": provider_pid,
            "provider_create_time": provider_create_time,
        },
    )


def clear_active_run(repo: Path, artifact_root: Path | None = None) -> None:
    path = active_run_path(repo, artifact_root)
    if path.is_file():
        path.unlink()


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def load_active_run(repo: Path, artifact_root: Path | None = None) -> ActiveRunRecord | None:
    path = active_run_path(repo, artifact_root)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Active run record must be an object: {path}")
    hostname = data.get("controller_hostname")
    return ActiveRunRecord(
        controller_pid=int(data["controller_pid"]),
        lifecycle_id=str(data["lifecycle_id"]),
        provider_pid=_optional_int(data.get("provider_pid")),
        controller_hostname=str(hostname) if hostname is not None else None,
        controller_started_at=_optional_float(data.get("controller_started_at")),
        provider_create_time=_optional_float(data.get("provider_create_time")),
    )


def persist_stopped_state(
    repo: Path,
    state: LifecycleState | None,
    artifact_root: Path | None = None,
) -> None:
    """Mark the lifecycle stopped without treating the turn as finished.

    The inflight marker stays in place so a later resume can reconcile the
    interrupted session instead of starting as if the turn never began.
    """
    if state is None:
        return
    state.status = LifecycleStatus.STOPPED
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state, artifact_root=artifact_root)


def controller_is_verified_local(
    pid: int,
    create_time: float | None,
    hostname: str | None,
) -> bool:
    """True when this host still runs the recorded controller process."""
    if pid <= 0:
        return False
    if hostname is None or hostname != socket.gethostname():
        return False
    if create_time is None:
        return False
    return process_matches(pid, create_time)


def controller_is_alive(pid: int, create_time: float | None) -> bool:
    """Deprecated alias; prefer :func:`controller_is_verified_local` with hostname."""
    return controller_is_verified_local(pid, create_time, None)


def provider_is_verified(record: ActiveRunRecord) -> bool:
    """True when the recorded provider PID is still the process Auto Loop started."""
    if record.provider_pid is None or record.provider_create_time is None:
        return False
    if record.controller_hostname is None or record.controller_hostname != socket.gethostname():
        return False
    return process_matches(record.provider_pid, record.provider_create_time)


def _local_hostname() -> str:
    return socket.gethostname()


def _ownership_blocks_local_reconciliation(
    active: ActiveRunRecord | None,
    lock,
) -> tuple[bool, bool, str | None]:
    """Return (remote, unverified, message) when local reconciliation must not run."""
    if active is not None and active.controller_hostname is not None:
        if active.controller_hostname != _local_hostname():
            host = active.controller_hostname
            return True, False, f"Lifecycle owned by another host ({host})."
    if lock is not None and lock.hostname != _local_hostname():
        return True, False, f"Lifecycle owned by another host ({lock.hostname})."
    if active is not None and active.controller_hostname is None:
        return False, True, "Active run metadata is unverified; not reconciled automatically."
    return False, False, None


def _read_active_run(
    repo: Path, artifact_root: Path | None
) -> tuple[ActiveRunRecord | None, bool]:
    path = active_run_path(repo, artifact_root)
    if not path.is_file():
        return None, False
    try:
        return load_active_run(repo, artifact_root), False
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, True


def _read_lock(repo: Path, artifact_root: Path | None):
    try:
        return load_workspace_lock(repo, artifact_root), False
    except LockError:
        return None, True


def _read_state(repo: Path, artifact_root: Path | None) -> LifecycleState | None:
    try:
        return load_lifecycle_state(repo, artifact_root)
    except RuntimeStateError:
        return None


def _reconcile_message(result: RuntimeReconcileResult) -> str:
    if result.message:
        return result.message
    if result.controller_alive and not result.changed:
        return "Controller is running."
    if not result.changed:
        return "No stale runtime ownership."
    parts: list[str] = []
    if result.provider_terminated:
        parts.append("stopped the orphan provider")
    if result.unverified_provider_skipped:
        parts.append("left an unverified provider PID running")
    if result.active_run_cleared:
        parts.append("cleared active_run.json")
    if result.lock_cleared:
        parts.append("cleared the workspace lock")
    if result.state_marked_stopped:
        parts.append("marked the lifecycle stopped")
    return "Reconciled stale runtime: " + "; ".join(parts) + "."


def reconcile_stale_runtime(
    repo: Path,
    artifact_root: Path | None = None,
    *,
    mark_unowned_running: bool = False,
) -> RuntimeReconcileResult:
    """Clean runtime ownership left by a dead controller.

    A live controller is left untouched. A dead controller's provider is
    killed only when its stored start time still matches. Stale ``active_run``
    and lock files are removed. A ``RUNNING`` lifecycle becomes ``STOPPED``
    with its inflight marker intact so resume can reconcile it.

    ``mark_unowned_running`` is for ``auto-loop stop`` when no ownership file
    remains but the lifecycle is still ``RUNNING``. ``run`` and ``resume``
    leave that state alone until ownership evidence says the controller died.
    """
    active, active_corrupt = _read_active_run(repo, artifact_root)
    lock, lock_invalid = _read_lock(repo, artifact_root)
    state = _read_state(repo, artifact_root)
    result = RuntimeReconcileResult()

    remote, unverified, blocked_message = _ownership_blocks_local_reconciliation(active, lock)
    if remote or unverified:
        result.remote_ownership = remote
        result.unverified_ownership = unverified
        result.controller_alive = remote
        result.message = blocked_message or "Ownership is not verified on this host."
        return result

    active_controller_alive = (
        active is not None
        and controller_is_verified_local(
            active.controller_pid,
            active.controller_started_at,
            active.controller_hostname,
        )
    )
    lock_verified_local = lock is not None and lock_owner_verified_local(lock)
    live_controller_pid: int | None = None
    if active_controller_alive and active is not None:
        live_controller_pid = active.controller_pid
    elif lock_verified_local and lock is not None:
        live_controller_pid = lock.pid
    result.controller_alive = live_controller_pid is not None

    if active_corrupt:
        clear_active_run(repo, artifact_root)
        result.active_run_cleared = True
    elif active is not None and not active_controller_alive:
        provider_pid = active.provider_pid
        if provider_is_verified(active) and provider_pid is not None:
            belongs_to_live = (
                live_controller_pid is not None and is_descendant(provider_pid, live_controller_pid)
            )
            if not belongs_to_live:
                terminated = terminate_process_tree(
                    provider_pid,
                    graceful_seconds=1.0,
                    expected_create_time=active.provider_create_time,
                )
                result.provider_terminated = terminated
        elif (
            provider_pid is not None
            and active.provider_create_time is None
            and is_pid_alive(provider_pid)
        ):
            result.unverified_provider_skipped = True
        clear_active_run(repo, artifact_root)
        result.active_run_cleared = True

    if lock is not None and not lock_blocks_workspace(lock) and not lock_invalid:
        lock_path(repo, artifact_root).unlink(missing_ok=True)
        result.lock_cleared = True

    ownership_evidence = result.active_run_cleared or result.lock_cleared
    if (
        state is not None
        and state.status == LifecycleStatus.RUNNING
        and live_controller_pid is None
        and (ownership_evidence or mark_unowned_running)
        and not lock_invalid
    ):
        persist_stopped_state(repo, state, artifact_root)
        result.state_marked_stopped = True

    result.message = _reconcile_message(result)
    return result


def _write_stop_message(text: str) -> None:
    try:
        os.write(2, f"\n{text}\n".encode())
    except OSError:
        return


@dataclass
class RunStopController:
    """In-process stop flag wired to SIGINT/SIGTERM.

    The handler only records the request and, when a provider PID is known,
    sends a non-blocking signal. Supervision performs the wait and cleanup.
    A second signal escalates that request to ``SIGKILL``.
    """

    repo: Path
    requested: bool = False
    force: bool = False
    active_provider_pid: int | None = None
    artifact_root: Path | None = None
    _signals: int = 0
    _previous_handlers: dict[int, object] = None  # type: ignore[assignment]

    def set_active_provider(self, pid: int | None) -> None:
        self.active_provider_pid = pid

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
        del signum
        self._signals += 1
        self.requested = True
        if self._signals == 1:
            _write_stop_message(STOPPING_MESSAGE)
            if self.active_provider_pid is not None:
                request_termination(self.active_provider_pid, force=False)
            return
        self.force = True
        if self._signals == 2:
            _write_stop_message(FORCE_STOPPING_MESSAGE)
        if self.active_provider_pid is not None:
            request_termination(self.active_provider_pid, force=True)


@dataclass(frozen=True)
class _VerifiedControllerTarget:
    pid: int
    create_time: float
    hostname: str


def _live_stop_target(
    active: ActiveRunRecord | None,
    lock,
) -> _VerifiedControllerTarget | None:
    if active is not None and controller_is_verified_local(
        active.controller_pid,
        active.controller_started_at,
        active.controller_hostname,
    ):
        started = active.controller_started_at
        host = active.controller_hostname
        if started is None or host is None:
            return None
        return _VerifiedControllerTarget(active.controller_pid, started, host)
    if lock is not None and lock_owner_verified_local(lock):
        started = lock.process_create_time
        if started is None:
            return None
        return _VerifiedControllerTarget(lock.pid, started, lock.hostname)
    return None


def _idle_stop_message(
    before: LifecycleStatus | None,
    result: RuntimeReconcileResult,
) -> str:
    if result.remote_ownership and result.message:
        return result.message
    if result.unverified_ownership and result.message:
        return result.message
    if result.provider_terminated:
        return "Stopped orphan provider and marked the lifecycle stopped."
    if result.state_marked_stopped:
        return "No active controller process; lifecycle marked stopped."
    if before == LifecycleStatus.STOPPED:
        return "Lifecycle is already stopped."
    if result.changed:
        return "Reconciled stale runtime ownership."
    return "No active auto-loop controller found for this workspace."


def request_remote_stop(
    repo: Path,
    *,
    wait_seconds: float = 2.0,
    artifact_root: Path | None = None,
) -> str:
    """Stop a live controller, or reconcile one that has already died.

    A live controller is signaled and given ``wait_seconds`` to persist
    ``STOPPED``. If it is still alive, the verified provider and then the
    controller are force-killed. A dead controller never takes the old
    "mark stopped and return" path while a verified provider is still running.
    """
    active, _corrupt = _read_active_run(repo, artifact_root)
    lock, _invalid = _read_lock(repo, artifact_root)
    before_state = _read_state(repo, artifact_root)
    before_status = before_state.status if before_state is not None else None
    remote, unverified, blocked_message = _ownership_blocks_local_reconciliation(active, lock)
    if remote or unverified:
        return blocked_message or "Ownership is not verified on this host."

    target = _live_stop_target(active, lock)
    if target is None:
        result = reconcile_stale_runtime(
            repo,
            artifact_root,
            mark_unowned_running=True,
        )
        return _idle_stop_message(before_status, result)

    target_pid = target.pid
    target_started = target.create_time
    try:
        os.kill(target_pid, signal.SIGTERM)
    except ProcessLookupError:
        result = reconcile_stale_runtime(
            repo,
            artifact_root,
            mark_unowned_running=True,
        )
        return _idle_stop_message(before_status, result)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        state = _read_state(repo, artifact_root)
        controller_gone = not controller_is_verified_local(
            target_pid, target_started, target.hostname
        )
        provider_alive = active is not None and provider_is_verified(active)
        stopped = state is not None and state.status == LifecycleStatus.STOPPED
        if stopped and controller_gone and not provider_alive:
            reconcile_stale_runtime(repo, artifact_root)
            return "Stop signal delivered; lifecycle is stopped."
        if controller_gone:
            break
        time.sleep(0.05)

    if (
        active is not None
        and active.controller_pid == target_pid
        and provider_is_verified(active)
        and active.provider_pid is not None
    ):
        terminate_process_tree(
            active.provider_pid,
            graceful_seconds=0.0,
            force=True,
            expected_create_time=active.provider_create_time,
        )
    if controller_is_verified_local(target_pid, target_started, target.hostname):
        try:
            os.kill(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    reconcile_stale_runtime(repo, artifact_root, mark_unowned_running=True)
    return "Stop signal delivered; lifecycle marked stopped."
