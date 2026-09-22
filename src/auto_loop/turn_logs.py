"""Per-turn provider raw/readable logs and retention."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from auto_loop.atomic_io import atomic_write_text
from auto_loop.config import AutoLoopConfig
from auto_loop.paths import auto_loop_root


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
    repo: Path
    config: AutoLoopConfig
    lifecycle_id: str
    turn: int
    role: str
    jsonl_path: Path = field(init=False)
    log_path: Path = field(init=False)
    _readable: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        artifact_root = self.repo / self.config.artifacts_root
        self.jsonl_path, self.log_path = turn_log_paths(
            self.repo, self.lifecycle_id, self.turn, self.role, artifact_root
        )
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    def write_stream_lines(self, lines: list[str]) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line.rstrip("\n") + "\n")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._readable.append(line.strip())
                    continue
                event_type = event.get("type") or event.get("event")
                if event_type == "result":
                    text = event.get("text") or event.get("result") or ""
                    if text:
                        self._readable.append(str(text).strip())
                elif event_type in ("tool_call", "tool_result"):
                    name = event.get("name") or event.get("tool")
                    self._readable.append(f"[tool] {name or event_type}")
                elif event_type == "assistant":
                    chunk = event.get("text") or event.get("content") or ""
                    if chunk:
                        self._readable.append(str(chunk).strip())

    def finalize(self) -> None:
        body = "\n".join(line for line in self._readable if line).strip()
        if not body:
            body = "(no readable provider output captured)"
        atomic_write_text(self.log_path, body + "\n")


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
    roles = [role] if role else ("worker", "reviewer")
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
    """Return a role name that has a log file for ``turn`` (worker first)."""
    run_dir = run_logs_dir(repo, lifecycle_id, artifact_root)
    if not run_dir.is_dir():
        return None
    suffix = ".jsonl" if raw else ".log"
    for role in ("worker", "reviewer"):
        if (run_dir / f"turn-{turn:04d}-{role}{suffix}").is_file():
            return role
    return None
