"""Thinking and message prefixes once per contiguous block."""

import json
import re
from io import StringIO
from pathlib import Path

from auto_loop.config import default_config
from auto_loop.console_output import RunConsole
from auto_loop.protocol import RESULT_BLOCK_END, RESULT_BLOCK_START
from auto_loop.providers.cursor import TraceEvent, TraceEventKind
from auto_loop.trace_text_block import TraceTextBlock, continuation_prefix
from auto_loop.turn_logs import TurnLogWriter

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MESSAGE = "[message] "
_THINKING = "[thinking] "
_MESSAGE_INDENT = continuation_prefix(TraceEventKind.MESSAGE)
_THINKING_INDENT = continuation_prefix(TraceEventKind.THINKING)


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


def _message(text: str, *, stamp: int = 1) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp_ms": stamp,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def _thinking(text: str) -> str:
    return json.dumps({"type": "thinking", "text": text})


def _system() -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"})


def _tool_start(path: str = "src/auto_loop/loop.py") -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "status": "running",
            "name": "read_file",
            "args": {"path": path},
        }
    )


def _tool_end(result: str = "ok") -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "status": "completed",
            "name": "read_file",
            "result": result,
        }
    )


def _drive(
    tmp_path: Path,
    lines: list[str],
    *,
    attempts: list[list[str]] | None = None,
    level: str = "normal",
    color: bool | None = False,
    stream: StringIO | None = None,
) -> tuple[str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    writer = TurnLogWriter(repo, default_config(), "lc-block", 1, "worker")
    target = stream or StringIO()
    console = RunConsole(level, stream=target, color=color)
    groups = attempts if attempts is not None else [lines]
    for group in groups:
        for line in group:
            for event in writer.write_stream_line(line):
                console.provider_trace(event)
        writer.finish_provider_attempt()
        console.finish_provider_trace()
    writer.finalize()
    console.finish_provider_trace()
    readable = writer.log_path.read_text(encoding="utf-8")
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    return target.getvalue(), readable, raw


def _assert_match(console: str, readable: str, expected: str) -> None:
    assert console == expected
    assert readable == expected


def test_renderer_closes_only_an_open_line():
    block = TraceTextBlock()
    assert block.feed(TraceEventKind.MESSAGE, "") == ""
    assert block.close() == ""
    assert block.feed(TraceEventKind.MESSAGE, "hello") == "[message] hello"
    assert block.close() == "\n"
    assert block.close() == ""


def test_same_kind_stream_chunks_share_one_prefix(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_message("hello ", stamp=1), _message("world", stamp=2)],
    )
    _assert_match(console, readable, "[message] hello world\n")


def test_multiline_message_indents_continuations(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_message("line 1\nline 2\nline 3")],
    )
    _assert_match(
        console,
        readable,
        f"{_MESSAGE}line 1\n{_MESSAGE_INDENT}line 2\n{_MESSAGE_INDENT}line 3\n",
    )
    assert readable.count("[message]") == 1


def test_multiline_thinking_indents_continuations(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_thinking("inspect first\ncheck tests\nlook at resume")],
    )
    _assert_match(
        console,
        readable,
        (
            f"{_THINKING}inspect first\n"
            f"{_THINKING_INDENT}check tests\n"
            f"{_THINKING_INDENT}look at resume\n"
        ),
    )
    assert readable.count("[thinking]") == 1


def test_kind_change_starts_a_new_block(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_thinking("reasoning"), _message("conclusion")],
    )
    _assert_match(console, readable, "[thinking] reasoning\n[message] conclusion\n")


def test_message_thinking_message_are_three_blocks(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [
            _message("answer line\ncontinuation", stamp=1),
            _thinking("further"),
            _message("done", stamp=2),
        ],
    )
    _assert_match(
        console,
        readable,
        (
            f"{_MESSAGE}answer line\n"
            f"{_MESSAGE_INDENT}continuation\n"
            "[thinking] further\n"
            "[message] done\n"
        ),
    )
    assert readable.count("[message]") == 2
    assert readable.count("[thinking]") == 1


def test_tool_boundary_restarts_the_message_prefix(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_message("before", stamp=1), _tool_start(), _tool_end(), _message("after", stamp=2)],
    )
    expected = (
        "[message] before\n"
        "[tool:start] read_file  src/auto_loop/loop.py\n"
        "[tool:end]   read_file  completed · 2 chars\n"
        "[message] after\n"
    )
    _assert_match(console, readable, expected)
    assert readable.count("[message]") == 2


def test_streamed_multiline_chunks_keep_continuation_indent(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_message("line 1\nli", stamp=1), _message("ne 2", stamp=2)],
    )
    _assert_match(
        console,
        readable,
        f"{_MESSAGE}line 1\n{_MESSAGE_INDENT}line 2\n",
    )


def test_blank_lines_are_preserved_without_a_prefix(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [_message("Summary:\n\nDetails follow.")],
    )
    expected = f"{_MESSAGE}Summary:\n\n{_MESSAGE_INDENT}Details follow.\n"
    _assert_match(console, readable, expected)
    lines = readable.splitlines()
    assert lines[0] == "[message] Summary:"
    assert lines[1] == ""
    assert lines[2] == f"{_MESSAGE_INDENT}Details follow."
    assert "[message]" not in lines[1]


def test_blank_lines_split_across_chunks_stay_unprefixed(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [
            _message("Summary:\n", stamp=1),
            _message("\n", stamp=2),
            _message("Details follow.", stamp=3),
        ],
    )
    _assert_match(
        console,
        readable,
        f"{_MESSAGE}Summary:\n\n{_MESSAGE_INDENT}Details follow.\n",
    )


def test_result_block_hides_protocol_json_and_keeps_one_prefix(tmp_path: Path):
    narrative = "Review complete.\n"
    payload = f'{narrative}{RESULT_BLOCK_START}\n{{"schema_version": 2}}\n{RESULT_BLOCK_END}'
    console, readable, raw = _drive(tmp_path, [_message(payload)])
    expected = "[message] Review complete.\n"
    _assert_match(console, readable, expected)
    assert readable.count("[message]") == 1
    assert "schema_version" not in readable
    assert RESULT_BLOCK_START not in readable
    assert RESULT_BLOCK_START in raw


def test_result_marker_split_across_deltas_stays_hidden(tmp_path: Path):
    console, readable, raw = _drive(
        tmp_path,
        [
            _message("Review complete.\n<AUTO_LOOP_RE", stamp=1),
            _message(f'SULT>\n{{"x": 1}}\n{RESULT_BLOCK_END}', stamp=2),
        ],
    )
    _assert_match(console, readable, "[message] Review complete.\n")
    assert "AUTO_LOOP_RESULT" not in readable
    assert '"x": 1' not in readable
    assert "<AUTO_LOOP_RE" in raw
    assert "SULT>" in raw


def test_pending_lt_before_tool_flushes_then_starts_a_new_message(tmp_path: Path):
    console, readable, _raw = _drive(
        tmp_path,
        [
            _message("value is <", stamp=1),
            _tool_start("src/auto_loop/review_targets.py"),
            _message("5", stamp=2),
        ],
    )
    expected = (
        "[message] value is <\n"
        "[tool:start] read_file  src/auto_loop/review_targets.py\n"
        "[message] 5\n"
    )
    _assert_match(console, readable, expected)
    before_tool = readable.split("[tool:", 1)[0]
    assert before_tool.count("[message]") == 1
    assert "value is <" in before_tool


def test_ordinary_lt_across_a_newline_stays_in_one_block(tmp_path: Path):
    console, readable, _raw = _drive(tmp_path, [_message("comparison <\nnext")])
    _assert_match(
        console,
        readable,
        f"{_MESSAGE}comparison <\n{_MESSAGE_INDENT}next\n",
    )


def test_provider_retry_starts_a_fresh_prefix(tmp_path: Path):
    partial = f'visible narrative\n{RESULT_BLOCK_START}\n{{"schema_version": 2, "actor": "reviewer"'
    complete = "retry succeeded"
    console, readable, raw = _drive(
        tmp_path,
        [],
        attempts=[
            [_system(), _message(partial, stamp=1)],
            [_system(), _message(complete, stamp=2)],
        ],
    )
    expected = "[message] visible narrative\n[message] retry succeeded\n"
    _assert_match(console, readable, expected)
    assert "schema_version" not in readable
    assert readable.count("[message]") == 2
    assert f"{_MESSAGE_INDENT}retry succeeded" not in readable
    assert raw.count(RESULT_BLOCK_START) == 1
    assert "retry succeeded" in raw


def test_turn_boundary_resets_text_block_state():
    stream = StringIO()
    console = RunConsole("normal", stream=stream, color=False)
    console.turn_started(1, "planner", model="planner-model")
    console.provider_trace(
        TraceEvent(TraceEventKind.MESSAGE, text="line 1\nline 2\n")
    )
    console.turn_started(2, "plan_reviewer", model="reviewer-model")
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="fresh"))
    console.finish_provider_trace()
    text = stream.getvalue()
    assert "Turn 1 · PLANNER · planner-model" in text
    assert "Turn 2 · PLAN REVIEWER · reviewer-model" in text
    assert f"{_MESSAGE}line 1\n{_MESSAGE_INDENT}line 2\n" in text
    assert f"{_MESSAGE}fresh\n" in text
    assert f"{_MESSAGE_INDENT}fresh" not in text
    assert text.count("[message]") == 2


def test_readable_log_and_console_share_block_structure(tmp_path: Path):
    body = "x" * 8421
    lines = [
        _thinking(
            "I need to inspect the worker transition.\n"
            "The review target validation is in review_targets.py."
        ),
        _tool_start("src/auto_loop/review_targets.py"),
        _tool_end(body),
        _thinking("The protected-path rule is correct."),
        _message(
            "The request is repairable.\nResume should reopen the same worker session.",
            stamp=2,
        ),
    ]
    console, readable, raw = _drive(tmp_path, lines)
    expected = (
        f"{_THINKING}I need to inspect the worker transition.\n"
        f"{_THINKING_INDENT}The review target validation is in review_targets.py.\n"
        "[tool:start] read_file  src/auto_loop/review_targets.py\n"
        "[tool:end]   read_file  completed · 8421 chars\n"
        "[thinking] The protected-path rule is correct.\n"
        f"{_MESSAGE}The request is repairable.\n"
        f"{_MESSAGE_INDENT}Resume should reopen the same worker session.\n"
    )
    _assert_match(console, readable, expected)
    assert readable.count("[thinking]") == 2
    assert readable.count("[message]") == 1
    assert body in raw
    assert body not in readable


def test_raw_jsonl_keeps_exact_provider_events(tmp_path: Path):
    lines = [
        _thinking("first\nsecond"),
        _message(f"Review complete.\n{RESULT_BLOCK_START}\n{{}}\n{RESULT_BLOCK_END}"),
    ]
    _console, readable, raw = _drive(tmp_path, lines)
    assert raw == "".join(f"{line}\n" for line in lines)
    assert "[thinking]" not in raw
    assert "[message]" not in raw
    assert RESULT_BLOCK_START in raw
    assert RESULT_BLOCK_START not in readable
    assert readable.count("[thinking]") == 1
    assert readable.count("[message]") == 1


def test_quiet_mode_suppresses_live_trace_but_still_writes_the_log(tmp_path: Path):
    console, readable, raw = _drive(
        tmp_path,
        [_message("line 1\nline 2")],
        level="quiet",
    )
    assert console == ""
    assert readable == f"{_MESSAGE}line 1\n{_MESSAGE_INDENT}line 2\n"
    assert "line 1\\nline 2" in raw or "line 1\nline 2" in raw


def test_verbose_mode_does_not_repeat_text_prefixes(tmp_path: Path):
    command = "p" * 500
    tool = json.dumps(
        {
            "type": "tool_call",
            "status": "running",
            "name": "run_terminal_cmd",
            "args": {"command": command},
        }
    )
    lines = [_thinking("inspect\nagain"), _message("Review complete:\n- tests pass"), tool]
    normal, normal_log, _raw = _drive(tmp_path / "n", lines, level="normal")
    verbose, verbose_log, _raw_verbose = _drive(tmp_path / "v", lines, level="verbose")
    assert normal == normal_log
    assert verbose.count("[thinking]") == 1
    assert verbose.count("[message]") == 1
    assert f"{_THINKING}inspect\n{_THINKING_INDENT}again\n" in verbose
    assert f"{_MESSAGE}Review complete:\n{_MESSAGE_INDENT}- tests pass\n" in verbose
    assert command not in normal
    assert command in verbose
    assert verbose_log.count("[message]") == 1
    assert command not in verbose_log


def test_no_color_and_non_tty_keep_plain_prefixes(tmp_path: Path, monkeypatch):
    lines = [_thinking("first\nsecond"), _message("one\ntwo")]
    non_tty, readable, _raw = _drive(tmp_path / "plain", lines, color=None)
    assert "\x1b[" not in non_tty
    assert non_tty == readable
    assert f"{_THINKING_INDENT}second" in non_tty
    assert f"{_MESSAGE_INDENT}two" in non_tty

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    tty = _TTY()
    colored_off, readable_off, _raw_off = _drive(
        tmp_path / "nocolor",
        lines,
        color=None,
        stream=tty,
    )
    assert colored_off == readable_off
    assert "\x1b[" not in colored_off
    assert f"{_THINKING}first\n{_THINKING_INDENT}second\n" in colored_off


def test_color_keeps_thinking_dim_and_plain_structure(tmp_path: Path):
    stream = StringIO()
    _colored, readable, _raw = _drive(
        tmp_path,
        [_thinking("first\nsecond"), _message("one\ntwo")],
        color=True,
        stream=stream,
    )
    colored = stream.getvalue()
    assert _ANSI.sub("", colored) == readable
    assert colored.count("\x1b[2m") >= 2
    assert "97" in colored
    assert readable.count("[thinking]") == 1
    assert readable.count("[message]") == 1
