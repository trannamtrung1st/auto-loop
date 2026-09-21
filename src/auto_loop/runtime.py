"""Lifecycle state persistence."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from auto_loop.atomic_io import atomic_write_json
from auto_loop.lifecycle import LifecycleState, enrich_lifecycle_state, migrate_lifecycle_data
from auto_loop.paths import auto_loop_root


class RuntimeStateError(Exception):
    """Lifecycle state could not be loaded or saved."""


def state_path(repo: Path) -> Path:
    return auto_loop_root(repo) / "runtime" / "state.json"


def load_lifecycle_state(repo: Path) -> LifecycleState | None:
    path = state_path(repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeStateError(f"Lifecycle state root must be an object: {path}")
        state = LifecycleState.model_validate(migrate_lifecycle_data(data))
        return enrich_lifecycle_state(repo, state)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise RuntimeStateError(f"Invalid lifecycle state at {path}: {exc}") from exc


def save_lifecycle_state(
    repo: Path,
    state: LifecycleState,
    *,
    before_replace=None,
) -> None:
    path = state_path(repo)
    payload = state.model_dump(mode="json")
    atomic_write_json(path, payload, before_replace=before_replace)
