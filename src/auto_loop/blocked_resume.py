"""Resume a lifecycle suspended by an external BLOCKED verdict."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

from auto_loop.config import AutoLoopConfig
from auto_loop.events import append_event, load_events
from auto_loop.git import GitProtocolError
from auto_loop.lifecycle import (
    BlockedResumeContext,
    LifecycleState,
    LifecycleStatus,
    utc_now,
)
from auto_loop.models import SessionSlot
from auto_loop.run_inputs import load_matching_blocked_record
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


def blocked_resume_context_from_record(
    record: BlockedRecord,
    state: LifecycleState,
) -> tuple[SessionSlot, BlockedResumeContext]:
    resume_session = resolve_resume_session(record, state)
    context = BlockedResumeContext(
        summary=record.summary,
        review_file=record.review_file,
        blocked_by_session=record.blocked_by_session or "reviewer",
        review_scope=record.review_scope,
        review_target=record.review_target,
        resume_session=resume_session,
    )
    return resume_session, context


def persist_unblock_state(state: LifecycleState, record: BlockedRecord) -> LifecycleState:
    """Apply in-memory unblock transition; caller must save before archiving."""
    resume_session, context = blocked_resume_context_from_record(record, state)
    state.status = LifecycleStatus.RUNNING
    state.next_session = resume_session
    state.blocked_resume_context = context
    state.updated_at = utc_now()
    return state


def _archive_dest(archive_dir: Path, record: BlockedRecord) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    micro = record.blocked_at.strftime("%Y%m%dT%H%M%S.%fZ")
    dest = archive_dir / f"blocked-{record.lifecycle_id}-turn{record.turn}-{micro}.json"
    if dest.exists():
        dest = archive_dir / f"{dest.stem}-{uuid4().hex[:8]}.json"
    return dest


def archive_blocked_record(
    repo: Path,
    record: BlockedRecord,
    artifact_root: Path | None = None,
) -> Path | None:
    source = blocked_path(repo, artifact_root)
    if not source.is_file():
        return None
    root = source.parent.parent
    archive_dir = root / "runtime" / "archive"
    dest = _archive_dest(archive_dir, record)
    shutil.move(str(source), str(dest))
    return dest


def _resumed_event_exists(
    repo: Path,
    config: AutoLoopConfig,
    *,
    lifecycle_id: str,
    turn: int,
) -> bool:
    for event in load_events(repo, config):
        if event.get("type") != "lifecycle_resumed_from_blocked":
            continue
        if event.get("lifecycle_id") == lifecycle_id and event.get("turn") == turn:
            return True
    return False


def _emit_resumed_from_blocked_event(
    repo: Path,
    config: AutoLoopConfig,
    record: BlockedRecord,
    resume_session: SessionSlot,
    archived: Path | None,
) -> None:
    if _resumed_event_exists(
        repo,
        config,
        lifecycle_id=record.lifecycle_id,
        turn=record.turn,
    ):
        return
    append_event(
        repo,
        config,
        {
            "type": "lifecycle_resumed_from_blocked",
            "lifecycle_id": record.lifecycle_id,
            "turn": record.turn,
            "resume_session": resume_session,
            "blocked_by_session": record.blocked_by_session,
            "summary": record.summary,
            "review_file": record.review_file,
            "archived_blocked_record": (
                _relative_to_repo(repo, archived) if archived is not None else None
            ),
        },
    )


def finalize_unblocked_archive(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    record: BlockedRecord,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Move blocked.json to archive and record the resume event (idempotent)."""
    resume_session = state.blocked_resume_context.resume_session if state.blocked_resume_context else resolve_resume_session(record, state)
    archived = archive_blocked_record(repo, record, artifact_root)
    _emit_resumed_from_blocked_event(repo, config, record, resume_session, archived)
    return state


def reconcile_blocked_resume(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Restart-safe blocked → running transition at lifecycle resume."""
    record = load_matching_blocked_record(repo, artifact_root)

    if state.status == LifecycleStatus.BLOCKED:
        if record is None:
            raise GitProtocolError(
                "Lifecycle is blocked but the suspension record is missing or stale. "
                "Inspect auto-loop status before retrying resume."
            )
        state = persist_unblock_state(state, record)
        save_lifecycle_state(repo, state, artifact_root=artifact_root)
        return finalize_unblocked_archive(
            repo, config, state, record, artifact_root=artifact_root
        )

    if state.blocked_resume_context is not None and record is not None:
        return finalize_unblocked_archive(
            repo, config, state, record, artifact_root=artifact_root
        )

    return state


def apply_resume_from_blocked(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    record: BlockedRecord,
    *,
    artifact_root: Path | None = None,
) -> LifecycleState:
    """Backward-compatible entry; prefer reconcile_blocked_resume on resume."""
    del record
    return reconcile_blocked_resume(repo, config, state, artifact_root=artifact_root)
