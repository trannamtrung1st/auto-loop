"""Provider supervision and retry tests."""

import json
import sys

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
