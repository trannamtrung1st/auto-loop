"""Subprocess supervision, timeouts, and provider retries."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from auto_loop.exits import ExitCode
from auto_loop.process import terminate_process_tree
from auto_loop.providers.cursor import (
    CursorStreamParseResult,
    SessionError,
    parse_cursor_stream,
)


class ProviderFailureKind(StrEnum):
    NONZERO_EXIT = "nonzero_exit"
    IDLE_TIMEOUT = "idle_timeout"
    WALL_TIMEOUT = "wall_timeout"
    INTERRUPTED = "interrupted"
    MISSING_RESULT = "missing_result"
    TRUNCATED = "truncated"


class ProviderError(Exception):
    """Provider infrastructure failure after retries are exhausted."""

    exit_code = ExitCode.PROVIDER_ERROR


@dataclass
class SupervisionOutcome:
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None
    failure: ProviderFailureKind | None = None
    timed_out: bool = False
    idle_timed_out: bool = False
    interrupted: bool = False
    parsed: CursorStreamParseResult | None = None


@dataclass
class RetryOutcome:
    attempts: int
    last: SupervisionOutcome
    session_id: str


@dataclass
class ProviderAttemptResult:
    """One supervised provider invocation for controller-owned retry policy."""

    lines: list[str]
    exit_code: int | None = None
    failure: ProviderFailureKind | None = None
    parsed: CursorStreamParseResult | None = None


def provider_attempt_from_outcome(outcome: SupervisionOutcome) -> ProviderAttemptResult:
    return ProviderAttemptResult(
        lines=list(outcome.lines),
        exit_code=outcome.exit_code,
        failure=outcome.failure,
        parsed=outcome.parsed,
    )


def provider_attempt_from_process_output(
    lines: list[str],
    exit_code: int,
    *,
    expected_session_id: str | None = None,
) -> ProviderAttemptResult:
    """Classify fake/scripted or replayed process output like a subprocess attempt."""
    outcome = SupervisionOutcome(lines=[line.rstrip("\n") for line in lines], exit_code=exit_code)
    if exit_code != 0:
        outcome.failure = ProviderFailureKind.NONZERO_EXIT
    classified = classify_stream_outcome(outcome, expected_session_id=expected_session_id)
    return provider_attempt_from_outcome(classified)


Clock = Callable[[], float]
LineIterator = Callable[[], str | None]
StopCheck = Callable[[], bool]
PidCallback = Callable[[int | None], None]
StreamLineCallback = Callable[[str], None]


def supervise_stream(
    next_line: LineIterator,
    *,
    clock: Clock = time.monotonic,
    wall_timeout_seconds: float,
    idle_timeout_seconds: float,
    stop_check: StopCheck | None = None,
    on_line: StreamLineCallback | None = None,
) -> SupervisionOutcome:
    """Read NDJSON lines until EOF or a timeout; refresh idle clock on each line.

    ``on_line`` runs on this caller after each dequeued stdout line is recorded,
    not from the stdout reader thread.
    """
    outcome = SupervisionOutcome()
    started = clock()
    last_activity = started

    while True:
        if stop_check and stop_check():
            outcome.failure = ProviderFailureKind.INTERRUPTED
            outcome.interrupted = True
            return outcome
        now = clock()
        if now - started > wall_timeout_seconds:
            outcome.failure = ProviderFailureKind.WALL_TIMEOUT
            outcome.timed_out = True
            return outcome
        if now - last_activity > idle_timeout_seconds:
            outcome.failure = ProviderFailureKind.IDLE_TIMEOUT
            outcome.idle_timed_out = True
            return outcome

        line = next_line()
        if line is None:
            if stop_check and stop_check():
                outcome.failure = ProviderFailureKind.INTERRUPTED
                outcome.interrupted = True
            return outcome
        if line == "":
            continue
        stored = line.rstrip("\n")
        outcome.lines.append(stored)
        if on_line is not None:
            on_line(stored)
        last_activity = clock()


def classify_stream_outcome(
    outcome: SupervisionOutcome,
    *,
    expected_session_id: str | None = None,
) -> SupervisionOutcome:
    if outcome.failure is not None:
        return outcome
    try:
        parsed = parse_cursor_stream(outcome.lines, expected_session_id=expected_session_id)
    except SessionError:
        raise
    except Exception:
        outcome.failure = ProviderFailureKind.TRUNCATED
        return outcome
    outcome.parsed = parsed
    if parsed.diagnostics or not parsed.final_text:
        outcome.failure = ProviderFailureKind.MISSING_RESULT
    return outcome


def run_subprocess_streaming(
    argv: list[str],
    *,
    wall_timeout_seconds: float,
    idle_timeout_seconds: float,
    graceful_seconds: float = 5.0,
    clock: Clock = time.monotonic,
    expected_session_id: str | None = None,
    stop_check: StopCheck | None = None,
    on_provider_pid: PidCallback | None = None,
    on_line: StreamLineCallback | None = None,
    poll_interval: float = 0.05,
) -> SupervisionOutcome:
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    if on_provider_pid is not None:
        on_provider_pid(proc.pid)

    line_queue: queue.Queue[str | None] = queue.Queue()
    eof_sent = threading.Event()

    def _stdout_reader() -> None:
        try:
            while True:
                line = proc.stdout.readline()
                if line == "":
                    break
                line_queue.put(line)
        finally:
            if not eof_sent.is_set():
                eof_sent.set()
                line_queue.put(None)

    def _stderr_drainer() -> None:
        try:
            while True:
                chunk = proc.stderr.read(8192)
                if not chunk:
                    break
        except OSError:
            return

    threading.Thread(target=_stdout_reader, daemon=True).start()
    threading.Thread(target=_stderr_drainer, daemon=True).start()

    def _read() -> str | None:
        if stop_check and stop_check():
            return None
        try:
            line = line_queue.get(timeout=poll_interval)
        except queue.Empty:
            return ""
        return line

    try:
        outcome = supervise_stream(
            _read,
            clock=clock,
            wall_timeout_seconds=wall_timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            stop_check=stop_check,
            on_line=on_line,
        )
        if outcome.failure in {
            ProviderFailureKind.IDLE_TIMEOUT,
            ProviderFailureKind.WALL_TIMEOUT,
            ProviderFailureKind.INTERRUPTED,
        }:
            terminate_process_tree(proc.pid, graceful_seconds=graceful_seconds)
            proc.wait(timeout=graceful_seconds)
            if outcome.failure == ProviderFailureKind.INTERRUPTED:
                outcome.interrupted = True
            return classify_stream_outcome(outcome, expected_session_id=expected_session_id)

        outcome.exit_code = proc.wait(timeout=graceful_seconds)
        if outcome.exit_code != 0 and outcome.failure is None:
            outcome.failure = ProviderFailureKind.NONZERO_EXIT
        return classify_stream_outcome(outcome, expected_session_id=expected_session_id)
    except KeyboardInterrupt:
        outcome = SupervisionOutcome(interrupted=True, failure=ProviderFailureKind.INTERRUPTED)
        terminate_process_tree(proc.pid, graceful_seconds=graceful_seconds)
        raise
    finally:
        if on_provider_pid is not None:
            on_provider_pid(None)
        if proc.poll() is None:
            terminate_process_tree(proc.pid, graceful_seconds=graceful_seconds)


_RETRYABLE_FAILURES = {
    ProviderFailureKind.NONZERO_EXIT,
    ProviderFailureKind.IDLE_TIMEOUT,
    ProviderFailureKind.WALL_TIMEOUT,
    ProviderFailureKind.MISSING_RESULT,
    ProviderFailureKind.TRUNCATED,
}


def is_retryable_failure(outcome: SupervisionOutcome) -> bool:
    return outcome.failure in _RETRYABLE_FAILURES


def is_retryable_provider_failure(kind: ProviderFailureKind | None) -> bool:
    return kind in _RETRYABLE_FAILURES


T = TypeVar("T")


def run_with_provider_retries(
    attempt: Callable[[int], SupervisionOutcome],
    *,
    provider_retries: int,
    session_id: str,
) -> RetryOutcome:
    """Retry infrastructure failures without rotating the persistent session ID."""
    max_attempts = 1 + max(provider_retries, 0)
    last = SupervisionOutcome()
    for attempt_index in range(1, max_attempts + 1):
        last = attempt(attempt_index)
        if last.failure is None:
            return RetryOutcome(attempts=attempt_index, last=last, session_id=session_id)
        if last.failure == ProviderFailureKind.INTERRUPTED:
            break
        if not is_retryable_failure(last) or attempt_index >= max_attempts:
            break
    raise ProviderError(
        f"Provider failed after {max_attempts} attempt(s) for session {session_id}: "
        f"{last.failure}"
    )


def iter_lines_from_list(lines: list[str]) -> LineIterator:
    index = 0

    def _next() -> str | None:
        nonlocal index
        if index >= len(lines):
            return None
        line = lines[index]
        index += 1
        return line + "\n"

    return _next


def fake_clock(start: float = 0.0, step: float = 0.0) -> tuple[Clock, Callable[[float], None]]:
    state = {"now": start}

    def now() -> float:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] += seconds

    return now, advance
