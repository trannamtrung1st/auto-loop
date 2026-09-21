"""CLI run option resolution."""

from __future__ import annotations

from dataclasses import dataclass

from auto_loop.config import AutoLoopConfig, ConsoleLevel


@dataclass(frozen=True)
class RunOptions:
    worker_model: str
    reviewer_model: str
    max_turns: int
    max_runtime_minutes: int
    verbose: bool
    quiet: bool
    console_level: ConsoleLevel = "normal"


def build_run_options(
    config: AutoLoopConfig,
    *,
    model: str | None = None,
    worker_model: str | None = None,
    reviewer_model: str | None = None,
    max_turns: int | None = None,
    max_runtime_minutes: int | None = None,
    verbose: bool = False,
    quiet: bool = False,
) -> RunOptions:
    default_model = model or "auto"
    return RunOptions(
        worker_model=worker_model or default_model,
        reviewer_model=reviewer_model or default_model,
        max_turns=max_turns if max_turns is not None else config.limits.max_turns,
        max_runtime_minutes=(
            max_runtime_minutes
            if max_runtime_minutes is not None
            else config.limits.max_runtime_minutes
        ),
        verbose=verbose,
        quiet=quiet,
        console_level=(
            "quiet"
            if quiet
            else ("verbose" if verbose else config.logging.console)
        ),
    )
