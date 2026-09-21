"""CLI logs rendering and optional follow mode."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from auto_loop.lifecycle import LifecycleStatus
from auto_loop.runtime import load_lifecycle_state
from auto_loop.turn_logs import (
    latest_turn_with_logs,
    read_turn_logs,
    role_with_log_for_turn,
    turn_log_paths,
)

WriteChunk = Callable[[str], None]


def _default_stdout_write(chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()


def stream_follow_logs(
    repo: Path,
    *,
    turn: int | None = None,
    raw: bool = False,
    write: WriteChunk | None = None,
    follow_max_seconds: float | None = None,
    follow_idle_seconds: float = 1.0,
) -> None:
    """Write turn log bytes to ``write`` as they appear (proposal §35 logs --follow)."""
    emit = write or _default_stdout_write
    state = load_lifecycle_state(repo)
    if state is None:
        emit("No lifecycle state; no turn logs available.")
        return
    lifecycle_id = state.lifecycle_id
    selected = turn if turn is not None else latest_turn_with_logs(repo, lifecycle_id)
    if selected is None:
        emit("No turn logs recorded yet.")
        return

    role = role_with_log_for_turn(repo, lifecycle_id, selected, raw=raw)
    if role is None:
        emit(f"No turn logs recorded for turn {selected}.")
        return
    jsonl_path, log_path = turn_log_paths(repo, lifecycle_id, selected, role)
    target = jsonl_path if raw else log_path
    if not target.is_file():
        emit(f"Waiting for {target.name}...")
        last_size = 0
    else:
        emit(f"=== turn {selected:04d} {role} ===\n")
        last_size = 0

    started = time.monotonic()
    last_growth = started
    try:
        while True:
            grew = False
            if target.is_file():
                text = target.read_text(encoding="utf-8")
                if len(text) > last_size:
                    emit(text[last_size:])
                    last_size = len(text)
                    grew = True
                    last_growth = time.monotonic()
            current = load_lifecycle_state(repo)
            terminal = current is None or current.status != LifecycleStatus.RUNNING
            if terminal and not grew and time.monotonic() - last_growth >= follow_idle_seconds:
                break
            if follow_max_seconds is not None and time.monotonic() - started >= follow_max_seconds:
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass


def render_logs(
    repo: Path,
    *,
    turn: int | None = None,
    raw: bool = False,
    follow: bool = False,
    follow_max_seconds: float | None = None,
    follow_idle_seconds: float = 1.0,
) -> str:
    state = load_lifecycle_state(repo)
    if state is None:
        return "No lifecycle state; no turn logs available."
    lifecycle_id = state.lifecycle_id
    selected = turn if turn is not None else latest_turn_with_logs(repo, lifecycle_id)
    if selected is None:
        return "No turn logs recorded yet."

    if follow:
        from io import StringIO

        buffer = StringIO()

        def capture(chunk: str) -> None:
            buffer.write(chunk)

        stream_follow_logs(
            repo,
            turn=turn,
            raw=raw,
            write=capture,
            follow_max_seconds=follow_max_seconds,
            follow_idle_seconds=follow_idle_seconds,
        )
        return buffer.getvalue().rstrip("\n") if buffer.tell() else buffer.getvalue()

    return read_turn_logs(repo, lifecycle_id, selected, raw=raw)
