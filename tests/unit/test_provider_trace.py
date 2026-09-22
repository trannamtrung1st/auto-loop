"""Normalized Cursor stream trace and live persistence through the controller."""

import json
import subprocess
from io import StringIO
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.console_output import RunConsole
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import create_lifecycle
from auto_loop.loop import LifecycleRunner
from auto_loop.providers.cursor import (
    TraceEventKind,
    format_tool_trace,
    trace_events_from_stream_line,
)
from auto_loop.providers.supervision import ProviderAttemptResult, provider_attempt_from_process_output
from auto_loop.run_options import RunOptions
from auto_loop.runtime import save_lifecycle_state
from auto_loop.turn_logs import turn_log_paths


def _events(payload: dict) -> list:
    return trace_events_from_stream_line(json.dumps(payload))


def test_thinking_deltas_are_text_events():
    events = [
        *_events({"type": "thinking", "text": "I need "}),
        *_events({"type": "thinking", "text": "to inspect "}),
        *_events({"type": "thinking", "text": "the repository."}),
    ]
    assert [event.kind for event in events] == [TraceEventKind.THINKING] * 3
    assert "".join(event.text for event in events) == "I need to inspect the repository."


def test_assistant_text_blocks_ignore_tool_use():
    events = _events(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "look first"},
                    {"type": "text", "text": "hello "},
                    {"type": "tool_use", "name": "read_file", "input": {"path": "x"}},
                    {"type": "text", "text": "world"},
                ]
            },
        }
    )
    assert [event.kind for event in events] == [
        TraceEventKind.THINKING,
        TraceEventKind.MESSAGE,
        TraceEventKind.MESSAGE,
    ]
    assert events[0].text == "look first"
    assert "".join(event.text for event in events[1:]) == "hello world"


def test_assistant_string_content_is_a_message():
    events = _events({"type": "assistant", "message": {"content": "partial "}})
    assert len(events) == 1
    assert events[0].kind is TraceEventKind.MESSAGE
    assert events[0].text == "partial "


def test_tool_call_status_maps_to_start_and_end():
    started = _events(
        {
            "type": "tool_call",
            "status": "running",
            "name": "read_file",
            "call_id": "call-1",
            "args": {"path": "src/auto_loop/loop.py"},
        }
    )
    completed = _events(
        {
            "type": "tool_call",
            "status": "completed",
            "name": "read_file",
            "call_id": "call-1",
            "result": "x" * 18432,
        }
    )
    failed = _events(
        {
            "type": "tool_call",
            "status": "error",
            "name": "read_file",
            "call_id": "call-1",
            "result": "not found",
        }
    )
    assert started[0].kind is TraceEventKind.TOOL_START
    assert started[0].tool_name == "read_file"
    assert started[0].call_id == "call-1"
    assert started[0].args == {"path": "src/auto_loop/loop.py"}
    assert completed[0].kind is TraceEventKind.TOOL_END
    assert completed[0].status == "completed"
    assert failed[0].status == "error"
    assert format_tool_trace(started[0]) == (
        '[tool:start] read_file  {"path":"src/auto_loop/loop.py"}'
    )
    assert format_tool_trace(completed[0]) == (
        "[tool:end]   read_file  completed · 18432 chars"
    )
    assert format_tool_trace(failed[0]) == "[tool:end]   read_file  error · not found"


def test_nested_cli_tool_call_is_generic():
    started = _events(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "toolu_01",
            "tool_call": {"readToolCall": {"args": {"path": "src/auto_loop/loop.py"}}},
        }
    )
    failed = _events(
        {
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "toolu_01",
            "tool_call": {"readToolCall": {"result": {"error": {"errorMessage": "missing"}}}},
        }
    )
    assert started[0].kind is TraceEventKind.TOOL_START
    assert started[0].tool_name == "readToolCall"
    assert started[0].status == "running"
    assert failed[0].kind is TraceEventKind.TOOL_END
    assert failed[0].status == "error"
    assert "missing" in format_tool_trace(failed[0])


def test_malformed_system_result_and_tool_result_do_not_crash():
    assert trace_events_from_stream_line("{not-json") == []
    assert trace_events_from_stream_line("[]") == []
    assert trace_events_from_stream_line("") == []
    assert _events({"type": "system", "session_id": "s"}) == []
    assert _events({"type": "result", "result": "done"}) == []
    assert _events({"type": "tool_result", "name": "read_file", "result": "secret"}) == []


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )


class _StreamingInvoker:
    def __init__(
        self,
        attempts: list[list[str]],
        console_stream: StringIO,
        log_path: Path,
        jsonl_path: Path,
    ) -> None:
        self.attempts = attempts
        self.on_stream_line = None
        self.console_stream = console_stream
        self.log_path = log_path
        self.jsonl_path = jsonl_path
        self.rendered_during_invoke = False
        self.persisted_during_invoke = False

    def prepare(self, role: str) -> None:
        return None

    def invoke(self, argv: list[str]) -> ProviderAttemptResult:
        lines = self.attempts.pop(0)
        callback = self.on_stream_line
        if callback is not None:
            for line in lines:
                callback(line)
                if "[thinking]" in self.console_stream.getvalue():
                    self.rendered_during_invoke = True
                raw_text = (
                    self.jsonl_path.read_text(encoding="utf-8") if self.jsonl_path.is_file() else ""
                )
                log_text = self.log_path.read_text(encoding="utf-8") if self.log_path.is_file() else ""
                if "thinking" in raw_text and "[thinking]" in log_text:
                    self.persisted_during_invoke = True
        return provider_attempt_from_process_output(lines, 0)


def test_live_callback_persists_before_invoke_returns_and_retries_do_not_duplicate(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    bootstrap_workspace(repo)
    config = default_config()
    options = RunOptions(
        "auto",
        "auto",
        max_turns=2,
        max_runtime_minutes=60,
        verbose=False,
        quiet=False,
    )
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    jsonl_path, log_path = turn_log_paths(repo, state.lifecycle_id, state.turn, "planner")
    first = [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "thinking", "text": "one"}),
    ]
    second = [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({"type": "thinking", "text": "two"}),
        json.dumps({"type": "assistant", "text": "hello"}),
        json.dumps({"type": "result", "session_id": "sess-1", "result": "hello"}),
    ]
    stream = StringIO()
    invoker = _StreamingInvoker([first, second], stream, log_path, jsonl_path)
    runner = LifecycleRunner(repo, config, options, invoker)
    runner._console = RunConsole("normal", stream=stream, color=False)

    text = runner._invoke_slot("planner", "plan the task", state)

    assert text == "hello"
    assert invoker.rendered_during_invoke
    assert invoker.persisted_during_invoke
    raw = jsonl_path.read_text(encoding="utf-8")
    readable = log_path.read_text(encoding="utf-8")
    assert raw.count('"text": "one"') == 1
    assert raw.count('"text": "two"') == 1
    assert raw.count('"text": "hello"') == 1
    assert "[thinking] one" in readable
    assert "[thinking] two" in readable
    assert "[message] hello" in readable
    assert readable.count("[thinking]") == 2
    rendered = stream.getvalue()
    assert rendered.count("[thinking] one") == 1
    assert rendered.count("[thinking] two") == 1
    assert "[message] hello" in rendered
