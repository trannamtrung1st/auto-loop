"""Completion and blocked terminal records (proposal sections 37-38)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from auto_loop.config import AutoLoopConfig
from auto_loop.git import head_commit
from auto_loop.atomic_io import atomic_write_json
from auto_loop.paths import auto_loop_root
from auto_loop.product_state import is_product_tree_clean
from auto_loop.run_prerequisites import RunPreconditionError


class TerminalRecordError(Exception):
    """Terminal record could not be loaded or saved."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CompletionRecord(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["complete"] = "complete"
    completed_at: datetime
    lifecycle_id: str
    turn: int
    planner_session_id: str | None = None
    plan_reviewer_session_id: str | None = None
    worker_session_id: str | None
    reviewer_session_id: str | None
    planner_model: str | None = None
    worker_model: str | None = None
    reviewer_model: str | None = None
    initial_base_commit: str
    final_commit: str
    last_approved_commit: str
    final_review_file: str
    task_sha256: str
    plan_sha256: str | None = None
    initial_approved_plan_sha256: str | None = None


class BlockedRecord(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["blocked"] = "blocked"
    blocked_at: datetime
    lifecycle_id: str
    turn: int
    planner_session_id: str | None = None
    plan_reviewer_session_id: str | None = None
    worker_session_id: str | None
    reviewer_session_id: str | None
    summary: str
    review_file: str | None = None


def completion_path(repo: Path) -> Path:
    return auto_loop_root(repo) / "runtime" / "completion.json"


def blocked_path(repo: Path) -> Path:
    return auto_loop_root(repo) / "runtime" / "blocked.json"


def load_completion_record(repo: Path) -> CompletionRecord | None:
    path = completion_path(repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CompletionRecord.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise TerminalRecordError(f"Invalid completion record at {path}: {exc}") from exc


def save_completion_record(repo: Path, record: CompletionRecord) -> Path:
    path = completion_path(repo)
    atomic_write_json(path, record.model_dump(mode="json"))
    return path


def save_blocked_record(repo: Path, record: BlockedRecord) -> Path:
    path = blocked_path(repo)
    atomic_write_json(path, record.model_dump(mode="json"))
    return path


def load_blocked_record(repo: Path) -> BlockedRecord | None:
    path = blocked_path(repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BlockedRecord.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise TerminalRecordError(f"Invalid blocked record at {path}: {exc}") from exc


def task_and_plan_hashes(repo: Path, config: AutoLoopConfig) -> tuple[str, str | None]:
    task_path = repo / config.task_file
    if not task_path.is_file():
        raise RunPreconditionError(f"Missing task file: {config.task_file}")
    task_hash = _sha256_file(task_path)
    plan_path = repo / config.plan_file
    plan_hash = _sha256_file(plan_path) if plan_path.is_file() else None
    return task_hash, plan_hash


def completion_still_valid(repo: Path, config: AutoLoopConfig, record: CompletionRecord) -> bool:
    head = head_commit(repo)
    if head != record.final_commit:
        return False
    if not is_product_tree_clean(repo):
        return False
    task_hash, plan_hash = task_and_plan_hashes(repo, config)
    if task_hash != record.task_sha256:
        return False
    if record.plan_sha256 is not None and plan_hash != record.plan_sha256:
        return False
    return True


def assert_completion_inputs_unchanged(repo: Path, config: AutoLoopConfig, record: CompletionRecord) -> None:
    if completion_still_valid(repo, config, record):
        return
    raise RunPreconditionError(
        "This lifecycle completed earlier, but the task or product repository changed. "
        "Initialize or reset the lifecycle explicitly before running again."
    )


IDEMPOTENT_COMPLETE_MESSAGE = (
    "This lifecycle is already complete for the current task and repository state."
)
