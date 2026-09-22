"""Run/CLI terminal output deduplication."""

from auto_loop.cli import run_finish_line
from auto_loop.exits import ExitCode
from auto_loop.loop import RunOutcome


def test_run_finish_line_prefers_explicit_message():
    outcome = RunOutcome(
        exit_code=ExitCode.STOPPED,
        message="Run interrupted.",
        terminal_summary_rendered=False,
    )
    assert run_finish_line(outcome, quiet=False) == "Run interrupted."


def test_run_finish_line_skips_generic_when_console_rendered():
    outcome = RunOutcome(
        exit_code=ExitCode.COMPLETE,
        terminal_summary_rendered=True,
    )
    assert run_finish_line(outcome, quiet=False) is None


def test_run_finish_line_skips_generic_in_quiet_mode():
    outcome = RunOutcome(exit_code=ExitCode.COMPLETE)
    assert run_finish_line(outcome, quiet=True) is None


def test_run_finish_line_uses_generic_when_nothing_rendered():
    outcome = RunOutcome(exit_code=ExitCode.PROTOCOL_ERROR, message="bad protocol")
    assert run_finish_line(outcome, quiet=False) == "bad protocol"

    bare = RunOutcome(exit_code=ExitCode.SESSION_ERROR)
    assert run_finish_line(bare, quiet=False) == "Lifecycle finished with exit code 16"
