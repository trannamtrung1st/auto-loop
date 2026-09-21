"""logs --follow rendering."""

import subprocess
import threading
import time
from pathlib import Path

from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import LifecycleStatus, RoleSession, create_lifecycle
from auto_loop.logs_view import render_logs, stream_follow_logs
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


def test_stream_follow_emits_incremental_chunks_before_return(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions["worker"] = RoleSession(session_id="worker-session-1")
    save_lifecycle_state(repo, state)

    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 1, "worker")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"start"}\n', encoding="utf-8")
    log_path.write_text("line one\n", encoding="utf-8")

    chunks: list[str] = []
    seen_line_two_before_done = threading.Event()

    def capture(text: str) -> None:
        chunks.append(text)
        if "line two" in text:
            seen_line_two_before_done.set()

    def append_later() -> None:
        time.sleep(0.15)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("line two\n")

    threading.Thread(target=append_later, daemon=True).start()
    stream_follow_logs(
        repo,
        write=capture,
        follow_max_seconds=2.0,
        follow_idle_seconds=0.2,
    )
    body = "".join(chunks)
    assert "line one" in body
    assert "line two" in body
    assert seen_line_two_before_done.is_set()
    assert body.count("line one") == 1
    assert body.count("line two") == 1


def test_render_logs_follow_no_duplicate_body(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 1, "worker")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"done"}\n', encoding="utf-8")
    log_path.write_text("only once\n", encoding="utf-8")

    output = render_logs(repo, follow=True, follow_idle_seconds=0.15)
    assert output.count("only once") == 1


def test_stream_follow_uses_worker_log_when_next_actor_is_reviewer(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.next_actor = "reviewer"
    save_lifecycle_state(repo, state)

    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 1, "worker")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"w"}\n', encoding="utf-8")
    log_path.write_text("worker turn output\n", encoding="utf-8")

    chunks: list[str] = []
    stream_follow_logs(repo, write=chunks.append, follow_max_seconds=0.5, follow_idle_seconds=0.15)
    body = "".join(chunks)
    assert "worker turn output" in body
    assert "reviewer" not in body.split("===")[0]


def test_stream_follow_stops_after_terminal_lifecycle_idle(tmp_path: Path):
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
    chunks: list[str] = []

    stream_follow_logs(repo, write=chunks.append, follow_idle_seconds=0.15)
    assert "done" in "".join(chunks)
    assert time.monotonic() - started < 2.0
