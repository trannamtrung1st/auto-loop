"""Turn log retention and rendering."""

import subprocess
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.turn_logs import (
    TURN_LOG_ROLES,
    TurnLogWriter,
    list_turn_numbers,
    prune_run_history,
    read_turn_logs,
    role_with_log_for_turn,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_turn_logs_write_jsonl_and_readable(tmp_path: Path):
    repo = _repo(tmp_path)
    writer = TurnLogWriter(repo, default_config(), "lc-1", 1, "worker")
    writer.write_stream_lines(
        [
            '{"type":"result","text":"hello from provider"}',
        ]
    )
    writer.finalize()
    assert list_turn_numbers(repo, "lc-1") == [1]
    readable = read_turn_logs(repo, "lc-1", 1, raw=False)
    assert "hello from provider" in readable
    raw = read_turn_logs(repo, "lc-1", 1, raw=True)
    assert "result" in raw


def test_turn_log_roles_cover_all_session_purposes():
    assert TURN_LOG_ROLES == ("planner", "plan_reviewer", "worker", "reviewer")


def test_read_turn_logs_includes_planning_roles(tmp_path: Path):
    repo = _repo(tmp_path)
    for turn, role, body in (
        (1, "planner", "planner readable"),
        (2, "plan_reviewer", "plan reviewer readable"),
    ):
        writer = TurnLogWriter(repo, default_config(), "lc-1", turn, role)
        writer.write_stream_lines(['{"type":"assistant","text":"ignored"}'])
        writer.log_path.write_text(f"{body}\n", encoding="utf-8")
    assert "planner readable" in read_turn_logs(repo, "lc-1", 1)
    assert "plan reviewer readable" in read_turn_logs(repo, "lc-1", 2)


def test_role_with_log_for_turn_discovers_planning_roles(tmp_path: Path):
    repo = _repo(tmp_path)
    config = default_config()
    for turn, role in ((1, "planner"), (3, "plan_reviewer")):
        writer = TurnLogWriter(repo, config, "lc-1", turn, role)
        writer.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        writer.jsonl_path.write_text("{}\n", encoding="utf-8")
        writer.log_path.write_text("line\n", encoding="utf-8")
    assert role_with_log_for_turn(repo, "lc-1", 1) == "planner"
    assert role_with_log_for_turn(repo, "lc-1", 3) == "plan_reviewer"


def test_prune_run_history_keeps_current_lifecycle(tmp_path: Path):
    repo = _repo(tmp_path)
    config = default_config()
    config.logging.max_run_history = 1
    for lid in ("old-run", "current-run"):
        TurnLogWriter(repo, config, lid, 1, "worker").jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        (TurnLogWriter(repo, config, lid, 1, "worker").jsonl_path).write_text("line\n")
    prune_run_history(repo, config, "current-run")
    assert (repo / ".ai/auto-loop/runtime/runs/current-run").is_dir()
