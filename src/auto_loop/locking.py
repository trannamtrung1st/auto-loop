"""Workspace controller lock (proposal section 32)."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from auto_loop.atomic_io import atomic_write_json
from auto_loop.exits import ExitCode
from auto_loop.paths import auto_loop_root
from auto_loop.process import process_create_time, process_matches


class ConcurrentRunError(Exception):
    exit_code = ExitCode.CONCURRENT_RUN

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LockError(Exception):
    """Lock file could not be read or written."""


class WorkspaceLockRecord(BaseModel):
    pid: int
    hostname: str
    started_at: datetime
    lifecycle_id: str
    process_create_time: float | None = None


def lock_path(repo: Path, artifact_root: Path | None = None) -> Path:
    root = artifact_root if artifact_root is not None else auto_loop_root(repo)
    return root / "runtime" / "lock.json"


def load_workspace_lock(
    repo: Path, artifact_root: Path | None = None
) -> WorkspaceLockRecord | None:
    path = lock_path(repo, artifact_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorkspaceLockRecord.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise LockError(f"Invalid lock file at {path}: {exc}") from exc


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def lock_owner_verified_local(record: WorkspaceLockRecord) -> bool:
    """True when this machine still runs the lock owner's recorded process."""
    if record.hostname != socket.gethostname():
        return False
    if record.process_create_time is None:
        return False
    return process_matches(record.pid, record.process_create_time)


def lock_blocks_workspace(record: WorkspaceLockRecord) -> bool:
    """True when another run must not take over this workspace lock."""
    if record.hostname != socket.gethostname():
        return True
    return lock_owner_verified_local(record)


def lock_owner_is_live(record: WorkspaceLockRecord) -> bool:
    """Backward-compatible alias for workspace takeover and doctor warnings."""
    return lock_blocks_workspace(record)


@dataclass
class WorkspaceLockHandle:
    repo: Path
    record: WorkspaceLockRecord
    artifact_root: Path | None = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        path = lock_path(self.repo, self.artifact_root)
        try:
            current = load_workspace_lock(self.repo, self.artifact_root)
        except LockError:
            current = None
        if current and current.pid == self.record.pid and current.hostname == self.record.hostname:
            path.unlink(missing_ok=True)
        self.released = True


def acquire_workspace_lock(
    repo: Path,
    lifecycle_id: str,
    artifact_root: Path | None = None,
) -> WorkspaceLockHandle:
    path = lock_path(repo, artifact_root)
    existing = load_workspace_lock(repo, artifact_root)
    if existing is not None and lock_owner_is_live(existing):
        raise ConcurrentRunError(
            f"Another auto-loop controller owns this workspace "
            f"(pid={existing.pid}, host={existing.hostname}, lifecycle={existing.lifecycle_id})"
        )
    pid = os.getpid()
    record = WorkspaceLockRecord(
        pid=pid,
        hostname=socket.gethostname(),
        started_at=datetime.now().astimezone(),
        lifecycle_id=lifecycle_id,
        process_create_time=process_create_time(pid),
    )
    atomic_write_json(path, record.model_dump(mode="json"))
    return WorkspaceLockHandle(repo=repo, record=record, artifact_root=artifact_root)


def describe_lock_status(
    repo: Path, artifact_root: Path | None = None
) -> tuple[str, str]:
    """Return (severity, message) for doctor: ok, warning, or error."""
    try:
        record = load_workspace_lock(repo, artifact_root)
    except LockError as exc:
        return ("error", str(exc))
    if record is None:
        return ("ok", "No workspace lock held")
    if lock_owner_is_live(record):
        return (
            "warning",
            f"Active controller lock: pid={record.pid} host={record.hostname} "
            f"lifecycle={record.lifecycle_id}",
        )
    return (
        "warning",
        f"Stale workspace lock from pid={record.pid} host={record.hostname} "
        f"(eligible for takeover on next run)",
    )
