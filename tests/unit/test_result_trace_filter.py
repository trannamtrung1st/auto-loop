"""Display-only AUTO_LOOP_RESULT trace filtering."""

import json
from io import StringIO
from pathlib import Path

from auto_loop.console_output import RunConsole
from auto_loop.protocol import RESULT_BLOCK_END, RESULT_BLOCK_START
from auto_loop.providers.cursor import TraceEvent, TraceEventKind
from auto_loop.result_trace_filter import ResultTraceFilter
from auto_loop.turn_logs import TurnLogWriter


def test_filter_keeps_narrative_before_result_block():
    filt = ResultTraceFilter()
    before = filt.feed("Whole-task review against task.md:\n")
    inside = filt.feed(f"{RESULT_BLOCK_START}\n{{\"schema_version\": 2}}\n{RESULT_BLOCK_END}")
    assert "Whole-task" in before
    assert "schema_version" not in inside
    assert filt.flush() == ""


def test_filter_handles_split_opening_marker():
    filt = ResultTraceFilter()
    assert filt.feed("<AUTO_LOOP_RE") == ""
    assert filt.feed("SULT>\n{}") == ""
    assert filt.feed(RESULT_BLOCK_END) == ""


def test_filter_handles_split_closing_marker():
    filt = ResultTraceFilter()
    filt.feed(f"ok {RESULT_BLOCK_START} hidden ")
    assert filt.feed('{"x":1}') == ""
    assert filt.feed("</AUTO_LOOP_RES") == ""
    assert filt.feed("ULT>") == ""


def test_unterminated_block_does_not_leak_json():
    filt = ResultTraceFilter()
    assert filt.feed("note\n") == "note\n"
    assert filt.feed(f"{RESULT_BLOCK_START}\n{{\"secret\": true") == ""
    assert filt.flush() == ""


def test_flush_emits_incomplete_marker_prefix_when_not_inside_block():
    filt = ResultTraceFilter()
    assert filt.feed("partial <AUTO_LO") == "partial "
    assert filt.flush() == "<AUTO_LO"


def test_message_preserve_lt_across_newlines_without_marker():
    filt = ResultTraceFilter()
    assert filt.feed("comparison <\nnext") == "comparison <\nnext"
    assert filt.flush() == ""


def test_trailing_lt_preserved_at_end_of_turn():
    filt = ResultTraceFilter()
    assert filt.feed("value is <") == "value is "
    assert filt.flush() == "<"


def test_run_console_hides_result_block():
    stream = StringIO()
    console = RunConsole("normal", stream=stream, color=False)
    console.turn_started(1, "reviewer", model="auto")
    console.provider_trace(TraceEvent(kind=TraceEventKind.MESSAGE, text="Visible narrative. "))
    console.provider_trace(
        TraceEvent(
            kind=TraceEventKind.MESSAGE,
            text=f"{RESULT_BLOCK_START}\n{{\"actor\": \"reviewer\"}}\n{RESULT_BLOCK_END}",
        )
    )
    console.finish_provider_trace()
    text = stream.getvalue()
    assert "Visible narrative" in text
    assert "schema_version" not in text
    assert "AUTO_LOOP_RESULT" not in text


def test_turn_log_jsonl_retains_result_block(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    from auto_loop.config import default_config

    config = default_config()
    writer = TurnLogWriter(repo, config, "lc-1", 1, "reviewer")
    payload = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": f"Summary here.\n{RESULT_BLOCK_START}\n{{}}\n{RESULT_BLOCK_END}"}
            ]
        },
    }
    writer.write_stream_line(json.dumps(payload))
    writer.finalize()
    raw = writer.jsonl_path.read_text(encoding="utf-8")
    readable = writer.log_path.read_text(encoding="utf-8")
    assert RESULT_BLOCK_START in raw
    assert "Summary here" in readable
    assert RESULT_BLOCK_START not in readable
