"""logs --follow rendering."""

import subprocess
import threading
import time
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.init_cmd import bootstrap_workspace
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
    bootstrap_workspace(repo)
    return repo


def _write_role_turn_log(
    repo: Path,
    lifecycle_id: str,
    turn: int,
    role: str,
    body: str,
) -> None:
    jsonl_path, log_path = turn_log_paths(repo, lifecycle_id, turn, role)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text('{"type":"assistant","text":"x"}\n', encoding="utf-8")
    log_path.write_text(f"{body}\n", encoding="utf-8")


def test_render_logs_discovers_latest_planner_turn(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions["planner"] = RoleSession(session_id="p1", model="claude-4.5-sonnet")
    save_lifecycle_state(repo, state)
    _write_role_turn_log(repo, state.lifecycle_id, 3, "planner", "planner turn log")
    output = render_logs(repo, color=False)
    assert "planner turn log" in output
    assert "PLANNER" in output
    assert "turn 0003" in output
    assert "claude-4.5-sonnet" in output


def test_render_logs_readable_includes_session_model(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions["worker"] = RoleSession(session_id="w1", model="auto")
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    _write_role_turn_log(repo, state.lifecycle_id, 1, "worker", "worker body")
    output = render_logs(repo, color=False)
    assert "WORKER · auto" in output
    assert "worker body" in output


def test_stream_follow_readable_includes_session_model(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions["plan_reviewer"] = RoleSession(session_id="pr1", model="gpt-5.6")
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    _write_role_turn_log(
        repo, state.lifecycle_id, 2, "plan_reviewer", "plan reviewer follow log"
    )
    chunks: list[str] = []
    stream_follow_logs(repo, write=chunks.append, follow_idle_seconds=0.15, color=False)
    body = "".join(chunks)
    assert "gpt-5.6" in body
    assert "plan reviewer follow log" in body


def test_stream_follow_discovers_plan_reviewer_turn(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    _write_role_turn_log(
        repo, state.lifecycle_id, 2, "plan_reviewer", "plan reviewer follow log"
    )
    chunks: list[str] = []
    stream_follow_logs(repo, write=chunks.append, follow_idle_seconds=0.15, color=False)
    body = "".join(chunks)
    assert "plan reviewer follow log" in body
    assert "PLAN REVIEWER" in body
    assert "No turn logs recorded" not in body


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


def test_stream_follow_sees_live_thinking_message_and_tool_events(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit
    from auto_loop.turn_logs import TurnLogWriter

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    writer = TurnLogWriter(repo, default_config(), state.lifecycle_id, 1, "worker")
    writer.write_stream_line('{"type":"thinking","text":"I need "}')

    chunks: list[str] = []
    seen_tool = threading.Event()

    def capture(text: str) -> None:
        chunks.append(text)
        if "[tool:start]" in "".join(chunks):
            seen_tool.set()

    def append_later() -> None:
        time.sleep(0.15)
        writer.write_stream_line('{"type":"thinking","text":"to inspect"}')
        writer.write_stream_line(
            '{"type":"assistant","timestamp_ms":1,"message":{"content":[{"type":"text","text":"hello"}]}}'
        )
        writer.write_stream_line(
            '{"type":"tool_call","status":"running","name":"read_file","args":{"path":"src/a.py"}}'
        )
        state.status = LifecycleStatus.COMPLETED
        save_lifecycle_state(repo, state)

    threading.Thread(target=append_later, daemon=True).start()
    stream_follow_logs(
        repo,
        write=capture,
        follow_max_seconds=2.0,
        follow_idle_seconds=0.2,
        color=False,
    )
    body = "".join(chunks)
    assert seen_tool.is_set()
    assert "[thinking] I need to inspect" in body
    assert body.count("[thinking]") == 1
    assert "[message] hello" in body
    assert '[tool:start] read_file  src/a.py' in body
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    assert raw.count("\n") == 4
    assert "[thinking]" not in raw
    assert "[tool:start]" not in raw
    writer.finalize()
    assert writer.jsonl_path.read_text(encoding="utf-8") == raw


def test_stream_follow_waits_for_readable_log_after_raw_turn_starts(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit
    from auto_loop.turn_logs import TurnLogWriter

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    writer = TurnLogWriter(repo, default_config(), state.lifecycle_id, 1, "worker")
    writer.write_stream_line('{"type":"system","session_id":"sess-1"}')
    assert writer.jsonl_path.is_file()
    assert not writer.log_path.is_file()

    chunks: list[str] = []

    def append_later() -> None:
        time.sleep(0.15)
        writer.write_stream_line('{"type":"thinking","text":"now visible"}')
        writer.finish_open_trace()
        state.status = LifecycleStatus.COMPLETED
        save_lifecycle_state(repo, state)

    threading.Thread(target=append_later, daemon=True).start()
    stream_follow_logs(
        repo,
        write=chunks.append,
        follow_max_seconds=2.0,
        follow_idle_seconds=0.2,
        color=False,
    )
    body = "".join(chunks)
    assert "Waiting for" in body
    assert "[thinking] now visible" in body
    assert "WORKER" in body
    writer.finalize()


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


def _write_provider_log(
    repo: Path,
    *,
    text: str,
    raw_line: str | None = None,
    role: str = "reviewer",
    model: str = "gpt-5.6",
) -> None:
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    state.sessions[role] = RoleSession(session_id=f"{role}-1", model=model)
    state.status = LifecycleStatus.COMPLETED
    save_lifecycle_state(repo, state)
    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, 4, role)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.write_text(raw_line or '{"type":"assistant","text":"provider"}\n', encoding="utf-8")
    log_path.write_text(text, encoding="utf-8")


def test_raw_logs_keep_literal_provider_text_without_ansi(tmp_path: Path):
    repo = _repo(tmp_path)
    markup = "[bold red]not markup[/bold red]\n"
    _write_provider_log(repo, text="ignored readable\n", raw_line=markup)
    output = render_logs(repo, raw=True, color=True)
    assert "\x1b" not in output
    assert markup.strip() in output
    assert "=== turn 4 reviewer ===" in output
    assert "gpt-5.6" not in output
    assert "━" not in output


def test_human_logs_style_framing_and_leave_provider_text_literal(tmp_path: Path):
    repo = _repo(tmp_path)
    markup = "[bold red]not markup[/bold red]\n"
    _write_provider_log(repo, text=markup)
    plain = render_logs(repo, color=False)
    assert "\x1b" not in plain
    assert "REVIEWER" in plain
    assert "gpt-5.6" in plain
    assert "turn 0004" in plain
    assert markup.strip() in plain
    assert "━" in plain

    colored = render_logs(repo, color=True)
    assert "\x1b" in colored
    for line in colored.splitlines():
        if "not markup" in line:
            assert "\x1b" not in line
            assert markup.strip() in line


def test_follow_raw_streams_provider_text_without_new_ansi(tmp_path: Path):
    repo = _repo(tmp_path)
    markup = "[bold red]not markup[/bold red]\n"
    _write_provider_log(repo, text="readable\n", raw_line=markup)
    chunks: list[str] = []
    stream_follow_logs(
        repo,
        raw=True,
        write=chunks.append,
        follow_idle_seconds=0.15,
        color=True,
    )
    body = "".join(chunks)
    assert "\x1b" not in body
    assert markup.strip() in body
    assert "=== turn 0004 reviewer ===" in body
    provider_chunks = [chunk for chunk in chunks if "not markup" in chunk]
    assert provider_chunks
    assert all("\x1b" not in chunk for chunk in provider_chunks)


def test_follow_human_color_does_not_restyle_provider_chunks(tmp_path: Path):
    repo = _repo(tmp_path)
    markup = "[bold red]not markup[/bold red]\n"
    _write_provider_log(repo, text=markup)
    chunks: list[str] = []
    stream_follow_logs(
        repo,
        write=chunks.append,
        follow_idle_seconds=0.15,
        color=True,
    )
    body = "".join(chunks)
    assert "REVIEWER" in body
    assert markup.strip() in body
    provider_chunks = [chunk for chunk in chunks if "not markup" in chunk]
    assert provider_chunks
    assert all("\x1b" not in chunk for chunk in provider_chunks)
    assert any("\x1b" in chunk for chunk in chunks)


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
