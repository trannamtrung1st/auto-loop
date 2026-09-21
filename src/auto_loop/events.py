"""Append-only lifecycle event log (proposal section 33.1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_loop.config import AutoLoopConfig


def _event_path(repo: Path, config: AutoLoopConfig) -> Path:
    return repo / config.logging.event_log


def append_event(repo: Path, config: AutoLoopConfig, event: dict[str, Any]) -> None:
    path = _event_path(repo, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def load_events(repo: Path, config: AutoLoopConfig) -> list[dict[str, Any]]:
    path = _event_path(repo, config)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events
