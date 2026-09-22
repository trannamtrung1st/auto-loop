"""Turn log retention and rendering."""

import json
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
    write_unseen_stream_lines,
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
            json.dumps({"type": "thinking", "text": "I need "}),
            json.dumps({"type": "thinking", "text": "to inspect"}),
            json.dumps({"type": "assistant", "text": "hello "}),
            json.dumps({"type": "assistant", "text": "from provider"}),
        ]
    )
    partial = writer.log_path.read_text(encoding="utf-8")
    assert "[thinking] I need to inspect" in partial
    assert writer.raw_line_count == 4
    writer.finalize()
    assert list_turn_numbers(repo, "lc-1") == [1]
    readable = read_turn_logs(repo, "lc-1", 1, raw=False)
    assert "[thinking] I need to inspect" in readable
    assert "[message] hello from provider" in readable
    assert readable.count("[thinking]") == 1
    assert readable.count("[message]") == 1
    raw = read_turn_logs(repo, "lc-1", 1, raw=True)
    assert '"type": "thinking"' in raw
    assert "[thinking]" not in raw.split("===", 1)[-1]


def test_result_only_stream_stays_raw_and_readable_log_is_a_placeholder(tmp_path: Path):
    repo = _repo(tmp_path)
    writer = TurnLogWriter(repo, default_config(), "lc-1", 1, "worker")
    writer.write_stream_line(json.dumps({"type": "result", "text": "hello from provider"}))
    assert writer.jsonl_path.read_text(encoding="utf-8").count("hello from provider") == 1
    writer.finalize()
    assert writer.log_path.read_text(encoding="utf-8").strip() == "(no readable provider output captured)"


def test_malformed_line_is_kept_in_jsonl_without_breaking_the_trace(tmp_path: Path):
    repo = _repo(tmp_path)
    writer = TurnLogWriter(repo, default_config(), "lc-1", 1, "worker")
    writer.write_stream_line("{not-json")
    writer.write_stream_line(json.dumps({"type": "thinking", "text": "still here"}))
    writer.finalize()
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    assert "{not-json" in raw
    assert "[thinking] still here" in writer.log_path.read_text(encoding="utf-8")


def test_tool_payload_is_capped_in_the_readable_log(tmp_path: Path):
    repo = _repo(tmp_path)
    writer = TurnLogWriter(repo, default_config(), "lc-1", 1, "worker")
    command = "z" * 1000
    body = "x" * 18432
    writer.write_stream_line(
        json.dumps(
            {
                "type": "tool_call",
                "status": "running",
                "name": "run_terminal_cmd",
                "args": {"command": command},
            }
        )
    )
    writer.write_stream_line(
        json.dumps(
            {
                "type": "tool_call",
                "status": "completed",
                "name": "read_file",
                "result": body,
            }
        )
    )
    writer.finalize()
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    readable = writer.log_path.read_text(encoding="utf-8")
    assert command in raw
    assert body in raw
    assert command not in readable
    assert body not in readable
    assert "..." in readable
    assert "[tool:start] run_terminal_cmd" in readable
    assert "[tool:end]   read_file  completed · 18432 chars" in readable


def test_unseen_lines_do_not_duplicate_a_live_attempt(tmp_path: Path):
    repo = _repo(tmp_path)
    writer = TurnLogWriter(repo, default_config(), "lc-1", 7, "worker")
    first = [
        json.dumps({"type": "thinking", "text": "one"}),
        json.dumps({"type": "thinking", "text": " "}),
        json.dumps({"type": "thinking", "text": "pass"}),
    ]
    before = writer.raw_line_count
    for line in first:
        writer.write_stream_line(line)
    write_unseen_stream_lines(writer, first, before=before, on_line=writer.write_stream_line)
    writer.finish_open_trace()
    second = [json.dumps({"type": "assistant", "text": "next"})]
    before_second = writer.raw_line_count
    write_unseen_stream_lines(
        writer, second, before=before_second, on_line=writer.write_stream_line
    )
    writer.finalize()
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    readable = writer.log_path.read_text(encoding="utf-8")
    assert raw.count('"text": "one"') == 1
    assert raw.count('"text": "next"') == 1
    assert readable.count("[thinking]") == 1
    assert "[thinking] one pass" in readable
    assert "[message] next" in readable


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
