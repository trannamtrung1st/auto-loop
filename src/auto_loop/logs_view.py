"""CLI logs rendering and optional follow mode."""

from __future__ import annotations

import time
from pathlib import Path

from auto_loop.runtime import load_lifecycle_state
from auto_loop.turn_logs import latest_turn_with_logs, read_turn_logs, turn_log_paths


def render_logs(
    repo: Path,
    *,
    turn: int | None = None,
    raw: bool = False,
    follow: bool = False,
    follow_seconds: float = 0.0,
) -> str:
    state = load_lifecycle_state(repo)
    if state is None:
        return "No lifecycle state; no turn logs available."
    lifecycle_id = state.lifecycle_id
    selected = turn if turn is not None else latest_turn_with_logs(repo, lifecycle_id)
    if selected is None:
        return "No turn logs recorded yet."

    if not follow:
        return read_turn_logs(repo, lifecycle_id, selected, raw=raw)

    jsonl_path, log_path = turn_log_paths(repo, lifecycle_id, selected, state.next_actor)
    target = jsonl_path if raw else log_path
    if not target.is_file():
        return f"Waiting for {target.name}..."
    last_size = 0
    chunks: list[str] = []
    deadline = time.monotonic() + follow_seconds if follow_seconds > 0 else None
    while True:
        if target.is_file():
            text = target.read_text(encoding="utf-8")
            if len(text) > last_size:
                chunks.append(text[last_size:])
                last_size = len(text)
        if deadline is not None and time.monotonic() >= deadline:
            break
        if deadline is None:
            break
        time.sleep(0.05)
    prefix = read_turn_logs(repo, lifecycle_id, selected, raw=raw)
    tail = "".join(chunks)
    if prefix and tail:
        return f"{prefix}\n{tail}".strip()
    return (prefix or tail).strip()
