"""Turn log retention and rendering."""

import subprocess
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.turn_logs import TurnLogWriter, list_turn_numbers, prune_run_history, read_turn_logs


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


def test_prune_run_history_keeps_current_lifecycle(tmp_path: Path):
    repo = _repo(tmp_path)
    config = default_config()
    config.logging.max_run_history = 1
    for lid in ("old-run", "current-run"):
        TurnLogWriter(repo, config, lid, 1, "worker").jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        (TurnLogWriter(repo, config, lid, 1, "worker").jsonl_path).write_text("line\n")
    prune_run_history(repo, config, "current-run")
    assert (repo / ".ai/auto-loop/runtime/runs/current-run").is_dir()
