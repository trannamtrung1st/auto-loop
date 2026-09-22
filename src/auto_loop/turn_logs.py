"""Per-turn provider raw/readable logs and retention."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from auto_loop.atomic_io import atomic_write_text
from auto_loop.config import AutoLoopConfig
from auto_loop.paths import auto_loop_root
from auto_loop.providers.cursor import (
    TraceEvent,
    TraceEventKind,
    format_tool_trace,
    trace_events_from_stream_line,
    trace_text_prefix,
)

# Session slot names used in turn log filenames (matches TurnLogWriter ``role``).
TURN_LOG_ROLES: tuple[str, ...] = (
    "planner",
    "plan_reviewer",
    "worker",
    "reviewer",
)


def run_logs_dir(repo: Path, lifecycle_id: str, artifact_root: Path | None = None) -> Path:
    root = artifact_root if artifact_root is not None else auto_loop_root(repo)
    return root / "runtime" / "runs" / lifecycle_id


def turn_log_paths(
    repo: Path,
    lifecycle_id: str,
    turn: int,
    role: str,
    artifact_root: Path | None = None,
) -> tuple[Path, Path]:
    base = run_logs_dir(repo, lifecycle_id, artifact_root) / f"turn-{turn:04d}-{role}"
    return base.with_suffix(".jsonl"), base.with_suffix(".log")


@dataclass
class TurnLogWriter:
    """Append raw NDJSON and the normalized readable trace as each line arrives."""

    repo: Path
    config: AutoLoopConfig
    lifecycle_id: str
    turn: int
    role: str
    jsonl_path: Path = field(init=False)
    log_path: Path = field(init=False)
    _raw_line_count: int = field(default=0, init=False)
    _open_kind: TraceEventKind | None = field(default=None, init=False)
    _wrote_readable: bool = field(default=False, init=False)
    _jsonl_handle: TextIO | None = field(default=None, init=False, repr=False)
    _log_handle: TextIO | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        artifact_root = self.repo / self.config.artifacts_root
        self.jsonl_path, self.log_path = turn_log_paths(
            self.repo, self.lifecycle_id, self.turn, self.role, artifact_root
        )
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def raw_line_count(self) -> int:
        return self._raw_line_count

    def write_stream_line(self, line: str) -> list[TraceEvent]:
        """Persist one provider line immediately and return its trace events."""
        raw = line.rstrip("\n")
        self._append_jsonl(raw)
        events = trace_events_from_stream_line(raw)
        for event in events:
            self._write_trace_event(event)
        return events

    def write_stream_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.write_stream_line(line)

    def finish_open_trace(self) -> None:
        """Close an in-progress thinking or message line without ending the turn log."""
        if self._open_kind is None:
            return
        self._append_log("\n")
        self._open_kind = None

    def finalize(self) -> None:
        self.finish_open_trace()
        self._close_handles()
        if not self._wrote_readable:
            atomic_write_text(self.log_path, "(no readable provider output captured)\n")

    def _append_jsonl(self, raw: str) -> None:
        if self._jsonl_handle is None:
            self._jsonl_handle = self.jsonl_path.open("a", encoding="utf-8")
        self._jsonl_handle.write(raw + "\n")
        self._jsonl_handle.flush()
        self._raw_line_count += 1

    def _append_log(self, text: str) -> None:
        if self._log_handle is None:
            self._log_handle = self.log_path.open("a", encoding="utf-8")
        self._log_handle.write(text)
        self._log_handle.flush()
        self._wrote_readable = True

    def _write_trace_event(self, event: TraceEvent) -> None:
        if event.kind in (TraceEventKind.THINKING, TraceEventKind.MESSAGE):
            self._write_text_event(event)
            return
        if event.kind in (TraceEventKind.TOOL_START, TraceEventKind.TOOL_END):
            self.finish_open_trace()
            self._append_log(format_tool_trace(event) + "\n")

    def _write_text_event(self, event: TraceEvent) -> None:
        parts = event.text.split("\n")
        for index, part in enumerate(parts):
            if index:
                self.finish_open_trace()
            if not part:
                continue
            prefix = ""
            if self._open_kind is not event.kind:
                self.finish_open_trace()
                prefix = trace_text_prefix(event.kind)
                self._open_kind = event.kind
            self._append_log(prefix + part)

    def _close_handles(self) -> None:
        for handle in (self._jsonl_handle, self._log_handle):
            if handle is not None and not handle.closed:
                handle.flush()
                handle.close()
        self._jsonl_handle = None
        self._log_handle = None


def write_unseen_stream_lines(
    writer: TurnLogWriter,
    lines: list[str],
    *,
    before: int,
    on_line: Callable[[str], None],
) -> None:
    """Persist provider lines that were not already streamed through ``on_line``.

    Live providers append during ``invoke``. Scripted providers only return lines,
    so those still flow through ``on_line`` once ``invoke`` returns. Already
    streamed lines are skipped so a retry does not write them twice.
    """
    observed = writer.raw_line_count - before
    if observed < 0:
        observed = 0
    for line in lines[observed:]:
        on_line(line)


def prune_run_history(repo: Path, config: AutoLoopConfig, lifecycle_id: str) -> None:
    runs_root = repo / config.artifacts_root / "runtime" / "runs"
    if not runs_root.is_dir():
        return
    entries = sorted(
        [p for p in runs_root.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    limit = config.logging.max_run_history
    while len(entries) > limit:
        victim = entries.pop(0)
        if victim.name == lifecycle_id:
            continue
        shutil.rmtree(victim, ignore_errors=True)


def list_turn_numbers(
    repo: Path, lifecycle_id: str, artifact_root: Path | None = None
) -> list[int]:
    run_dir = run_logs_dir(repo, lifecycle_id, artifact_root)
    if not run_dir.is_dir():
        return []
    turns: set[int] = set()
    for path in run_dir.glob("turn-*-*.jsonl"):
        parts = path.name.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            turns.add(int(parts[1]))
    return sorted(turns)


def read_turn_logs(
    repo: Path,
    lifecycle_id: str,
    turn: int,
    *,
    raw: bool = False,
    role: str | None = None,
    artifact_root: Path | None = None,
    header: Callable[[int, str], str] | None = None,
) -> str:
    """Return turn log text.

    ``header``, when set, supplies Auto Loop framing for human-readable logs.
    Raw mode ignores it and keeps the legacy ``=== turn`` prefix so provider
    bytes stay literal.
    """
    run_dir = run_logs_dir(repo, lifecycle_id, artifact_root)
    if not run_dir.is_dir():
        return ""
    suffix = ".jsonl" if raw else ".log"
    roles = [role] if role else TURN_LOG_ROLES
    chunks: list[str] = []
    for r in roles:
        path = run_dir / f"turn-{turn:04d}-{r}{suffix}"
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        if header is not None and not raw:
            chunks.append(f"{header(turn, r)}{body}")
        else:
            chunks.append(f"=== turn {turn} {r} ===\n{body}")
    return "\n\n".join(chunks).strip()


def latest_turn_with_logs(
    repo: Path, lifecycle_id: str, artifact_root: Path | None = None
) -> int | None:
    turns = list_turn_numbers(repo, lifecycle_id, artifact_root)
    return turns[-1] if turns else None


def role_with_log_for_turn(
    repo: Path,
    lifecycle_id: str,
    turn: int,
    *,
    raw: bool = False,
    artifact_root: Path | None = None,
) -> str | None:
    """Return the first role (lifecycle order) that has a log file for ``turn``.

    Human mode prefers the readable log and falls back to raw JSONL so follow
    can attach while a turn has started but no trace line has been rendered yet.
    """
    run_dir = run_logs_dir(repo, lifecycle_id, artifact_root)
    if not run_dir.is_dir():
        return None
    suffixes = (".jsonl",) if raw else (".log", ".jsonl")
    for suffix in suffixes:
        for role in TURN_LOG_ROLES:
            if (run_dir / f"turn-{turn:04d}-{role}{suffix}").is_file():
                return role
    return None
