"""Provider supervision and retry tests."""

import json
import sys
import time

import pytest

from auto_loop.providers.supervision import (
    ProviderError,
    ProviderFailureKind,
    classify_stream_outcome,
    fake_clock,
    iter_lines_from_list,
    run_subprocess_streaming,
    run_with_provider_retries,
    supervise_stream,
)


def _result_line(text: str = "done") -> str:
    return json.dumps({"type": "result", "session_id": "sess-1", "result": text})


def test_supervise_stream_calls_on_line_before_the_next_read():
    seen: list[str] = []
    calls = {"n": 0}

    def next_line():
        calls["n"] += 1
        if calls["n"] == 1:
            return "first\n"
        if calls["n"] == 2:
            assert seen == ["first"]
            return "second\n"
        return None

    outcome = supervise_stream(
        next_line,
        wall_timeout_seconds=5,
        idle_timeout_seconds=5,
        on_line=seen.append,
    )
    assert seen == ["first", "second"]
    assert outcome.lines == ["first", "second"]


def test_supervise_stream_on_line_keeps_partial_output_on_timeout():
    clock, advance = fake_clock()
    seen: list[str] = []
    calls = {"n": 0}

    def next_line():
        calls["n"] += 1
        if calls["n"] == 1:
            return "partial\n"
        advance(50)
        return ""

    outcome = supervise_stream(
        next_line,
        clock=clock,
        wall_timeout_seconds=30,
        idle_timeout_seconds=100,
        on_line=seen.append,
    )
    assert seen == ["partial"]
    assert outcome.lines == ["partial"]
    assert outcome.failure == ProviderFailureKind.WALL_TIMEOUT


def test_supervise_stream_idle_timeout():
    clock, advance = fake_clock()
    sent = False

    def next_line():
        nonlocal sent
        if not sent:
            sent = True
            return "activity\n"
        advance(11)
        return ""

    outcome = supervise_stream(
        next_line,
        clock=clock,
        wall_timeout_seconds=200,
        idle_timeout_seconds=10,
    )
    assert outcome.failure == ProviderFailureKind.IDLE_TIMEOUT
    assert outcome.idle_timed_out


def test_supervise_stream_wall_timeout():
    clock, advance = fake_clock()

    def next_line():
        advance(50)
        return "still going\n"

    outcome = supervise_stream(
        next_line,
        clock=clock,
        wall_timeout_seconds=30,
        idle_timeout_seconds=100,
    )
    assert outcome.failure == ProviderFailureKind.WALL_TIMEOUT
    assert outcome.timed_out


def test_classify_missing_result():
    outcome = supervise_stream(
        iter_lines_from_list([json.dumps({"type": "system", "session_id": "s"})]),
        wall_timeout_seconds=10,
        idle_timeout_seconds=10,
    )
    classified = classify_stream_outcome(outcome)
    assert classified.failure == ProviderFailureKind.MISSING_RESULT


def test_classify_session_mismatch_is_not_truncated():
    from auto_loop.providers.cursor import SessionError

    outcome = supervise_stream(
        iter_lines_from_list([json.dumps({"type": "system", "session_id": "other"})]),
        wall_timeout_seconds=10,
        idle_timeout_seconds=10,
    )
    with pytest.raises(SessionError):
        classify_stream_outcome(outcome, expected_session_id="expected-sess")


def test_classify_success():
    outcome = supervise_stream(
        iter_lines_from_list([_result_line()]),
        wall_timeout_seconds=10,
        idle_timeout_seconds=10,
    )
    classified = classify_stream_outcome(outcome, expected_session_id="sess-1")
    assert classified.failure is None
    assert classified.parsed is not None
    assert classified.parsed.final_text == "done"


def test_provider_retries_exhausted():
    calls = 0

    def attempt(_index: int):
        nonlocal calls
        calls += 1
        from auto_loop.providers.supervision import SupervisionOutcome

        return SupervisionOutcome(failure=ProviderFailureKind.NONZERO_EXIT, exit_code=1)

    with pytest.raises(ProviderError):
        run_with_provider_retries(attempt, provider_retries=2, session_id="sess-1")
    assert calls == 3


def test_run_subprocess_streaming_reports_lines_before_the_child_exits():
    script = (
        "import json, time\n"
        "print(json.dumps({'type': 'thinking', 'text': 'hello '}), flush=True)\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'type': 'thinking', 'text': 'world'}), flush=True)\n"
        "print(json.dumps({'type': 'result', 'session_id': 's', 'result': 'done'}), flush=True)\n"
    )
    first: dict[str, float] = {}

    def on_line(line: str) -> None:
        if "hello" in line and "at" not in first:
            first["at"] = time.monotonic()

    outcome = run_subprocess_streaming(
        [sys.executable, "-c", script],
        wall_timeout_seconds=5.0,
        idle_timeout_seconds=5.0,
        on_line=on_line,
        poll_interval=0.05,
    )
    finished = time.monotonic()
    assert "at" in first
    # The child sleeps after the first line, so a callback that waited for exit
    # would see "hello" only at the end.
    assert finished - first["at"] > 0.3
    assert any("world" in line for line in outcome.lines)
    assert outcome.failure is None


def test_partial_subprocess_stream_is_persisted_on_idle_timeout(tmp_path):
    from auto_loop.config import default_config
    from auto_loop.turn_logs import TurnLogWriter

    repo = tmp_path / "repo"
    repo.mkdir()
    writer = TurnLogWriter(repo, default_config(), "lc-1", 1, "worker")
    script = (
        "import json, time\n"
        "print(json.dumps({'type': 'thinking', 'text': 'partial'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    outcome = run_subprocess_streaming(
        [sys.executable, "-c", script],
        wall_timeout_seconds=8.0,
        idle_timeout_seconds=1.5,
        on_line=writer.write_stream_line,
        poll_interval=0.05,
    )
    assert outcome.failure == ProviderFailureKind.IDLE_TIMEOUT
    assert writer.raw_line_count >= 1
    assert "[thinking] partial" in writer.log_path.read_text(encoding="utf-8")
    assert "thinking" in writer.jsonl_path.read_text(encoding="utf-8")
    writer.finalize()
    assert "[thinking] partial" in writer.log_path.read_text(encoding="utf-8")


def test_run_subprocess_streaming_idle_timeout_terminates_silent_child():
    outcome = run_subprocess_streaming(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=0.4,
        poll_interval=0.05,
    )
    assert outcome.failure == ProviderFailureKind.IDLE_TIMEOUT
    assert outcome.idle_timed_out


def test_run_subprocess_streaming_stop_check_interrupts_silent_child():
    outcome = run_subprocess_streaming(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=10.0,
        stop_check=lambda: True,
        poll_interval=0.05,
    )
    assert outcome.failure == ProviderFailureKind.INTERRUPTED
    assert outcome.interrupted


def test_provider_retries_stop_on_success():
    calls = 0

    def attempt(_index: int):
        nonlocal calls
        calls += 1
        from auto_loop.providers.supervision import SupervisionOutcome

        if calls < 2:
            return SupervisionOutcome(failure=ProviderFailureKind.IDLE_TIMEOUT, idle_timed_out=True)
        return classify_stream_outcome(
            supervise_stream(
                iter_lines_from_list([_result_line("ok")]),
                wall_timeout_seconds=5,
                idle_timeout_seconds=5,
            ),
            expected_session_id="sess-1",
        )

    result = run_with_provider_retries(attempt, provider_retries=2, session_id="sess-1")
    assert result.attempts == 2
    assert result.session_id == "sess-1"
    assert result.last.parsed is not None
