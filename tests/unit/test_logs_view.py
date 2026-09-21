"""logs --follow rendering."""

import subprocess
import threading
import time
from pathlib import Path

from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import LifecycleStatus, RoleSession, create_lifecycle
from auto_loop.logs_view import render_logs
from auto_loop.runtime import save_lifecycle_state
from auto_loop.turn_logs import turn_log_paths


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    run_init(repo)
    return repo


def test_render_logs_follow_observes_bytes_written_after_start(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions["worker"] = RoleSession(session_id="worker-session-1")
    state.sessions["reviewer"] = RoleSession(session_id="reviewer-session-1")
    save_lifecycle_state(repo, state)

    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 1, "worker")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"start"}\n', encoding="utf-8")
    log_path.write_text("line one\n", encoding="utf-8")

    def append_later() -> None:
        time.sleep(0.15)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("line two\n")

    threading.Thread(target=append_later, daemon=True).start()
    output = render_logs(
        repo,
        follow=True,
        follow_max_seconds=2.0,
        follow_idle_seconds=0.2,
    )
    assert "line one" in output
    assert "line two" in output


def test_render_logs_follow_stops_after_terminal_lifecycle_idle(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 1, "worker")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"done"}\n', encoding="utf-8")
    log_path.write_text("done\n", encoding="utf-8")

    started = time.monotonic()
    output = render_logs(repo, follow=True, follow_idle_seconds=0.15)
    assert "done" in output
    assert time.monotonic() - started < 2.0
