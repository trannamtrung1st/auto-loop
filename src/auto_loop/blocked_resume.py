"""Resume a lifecycle suspended by an external BLOCKED verdict."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.events import append_event
from auto_loop.git import GitProtocolError
from auto_loop.lifecycle import (
    BlockedResumeContext,
    LifecycleState,
    LifecycleStatus,
    utc_now,
)
from auto_loop.models import SessionSlot
from auto_loop.runtime import save_lifecycle_state
from auto_loop.terminal_records import BlockedRecord, blocked_path


def _relative_to_repo(repo: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def resolve_resume_session(record: BlockedRecord, state: LifecycleState) -> SessionSlot:
    if record.resume_session is not None:
        return record.resume_session
    if record.phase == "planning" or state.phase == "planning":
        return "planner"
    return "worker"


def archive_blocked_record(repo: Path, artifact_root: Path | None = None) -> Path | None:
    source = blocked_path(repo, artifact_root)
    if not source.is_file():
        return None
    root = source.parent.parent
    archive_dir = root / "runtime" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = archive_dir / f"blocked-{stamp}.json"
    shutil.move(str(source), str(dest))
    return dest


def apply_resume_from_blocked(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    record: BlockedRecord,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    if state.status != LifecycleStatus.BLOCKED:
        raise GitProtocolError(
            "Cannot resume from blocked: lifecycle status is "
            f"{state.status.value}, expected blocked."
        )
    resume_session = resolve_resume_session(record, state)
    archived = archive_blocked_record(repo, artifact_root)
    state.status = LifecycleStatus.RUNNING
    state.next_session = resume_session
    state.blocked_resume_context = BlockedResumeContext(
        summary=record.summary,
        review_file=record.review_file,
        blocked_by_session=record.blocked_by_session or "reviewer",
        review_scope=record.review_scope,
        review_target=record.review_target,
        resume_session=resume_session,
    )
    state.updated_at = utc_now()
    save_lifecycle_state(repo, state, artifact_root=artifact_root)
    append_event(
        repo,
        config,
        {
            "type": "lifecycle_resumed_from_blocked",
            "lifecycle_id": state.lifecycle_id,
            "resume_session": resume_session,
            "blocked_by_session": record.blocked_by_session,
            "summary": record.summary,
            "review_file": record.review_file,
            "archived_blocked_record": (
                _relative_to_repo(repo, archived) if archived is not None else None
            ),
        },
    )
    return state
