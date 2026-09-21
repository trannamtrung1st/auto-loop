"""Worker no-progress detection (proposal section 33.3)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.lifecycle import LifecycleState
from auto_loop.models import WorkerResult


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worker_progress_key(
    repo: Path,
    config: AutoLoopConfig,
    state: LifecycleState,
    result: WorkerResult,
    head: str,
) -> str:
    plan_path = repo / config.plan_file
    plan_hash = _sha256_file(plan_path) if plan_path.is_file() else ""
    if result.review:
        review_key = f"{result.review.scope}:{result.review.target}"
    else:
        review_key = "none"
    verification = "|".join(f"{v.command}:{v.result}" for v in result.verification)
    return (
        f"{head}|{plan_hash}|{result.status}|{review_key}|{verification}|{result.work_summary}"
    )


def update_worker_no_progress(
    state: LifecycleState,
    config: AutoLoopConfig,
    progress_key: str,
) -> bool:
    """Update streak; return True if no-progress limit exceeded."""
    if state.last_worker_progress_key == progress_key:
        state.worker_no_progress_streak += 1
    else:
        state.worker_no_progress_streak = 1
        state.last_worker_progress_key = progress_key
    return state.worker_no_progress_streak >= config.limits.max_consecutive_worker_no_progress
