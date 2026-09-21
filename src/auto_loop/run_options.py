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
    planner_model: str = "auto"
    goal_summary: str | None = None
    user_config_rel: str = "run.yaml"
    resuming: bool = False


def _role_model(
    *,
    role_cli: str | None,
    global_cli: str | None,
    config_model: str | None,
) -> str:
    if role_cli:
        return role_cli
    if global_cli:
        return global_cli
    if config_model:
        return config_model
    return "auto"


def build_run_options(
    config: AutoLoopConfig,
    *,
    model: str | None = None,
    planner_model: str | None = None,
    worker_model: str | None = None,
    reviewer_model: str | None = None,
    max_turns: int | None = None,
    max_runtime_minutes: int | None = None,
    verbose: bool = False,
    quiet: bool = False,
    goal_summary: str | None = None,
    user_config_rel: str = "run.yaml",
    resuming: bool = False,
) -> RunOptions:
    planner_cfg = config.agents.get("planner")
    worker_cfg = config.agents["worker"]
    reviewer_cfg = config.agents["reviewer"]
    return RunOptions(
        planner_model=_role_model(
            role_cli=planner_model,
            global_cli=model,
            config_model=planner_cfg.model if planner_cfg else None,
        ),
        worker_model=_role_model(
            role_cli=worker_model,
            global_cli=model,
            config_model=worker_cfg.model,
        ),
        reviewer_model=_role_model(
            role_cli=reviewer_model,
            global_cli=model,
            config_model=reviewer_cfg.model,
        ),
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
        goal_summary=goal_summary,
        user_config_rel=user_config_rel,
        resuming=resuming,
    )
