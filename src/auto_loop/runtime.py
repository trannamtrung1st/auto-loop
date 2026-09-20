"""Lifecycle state persistence."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from auto_loop.lifecycle import LifecycleState
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
        return LifecycleState.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise RuntimeStateError(f"Invalid lifecycle state at {path}: {exc}") from exc


def save_lifecycle_state(repo: Path, state: LifecycleState) -> None:
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
