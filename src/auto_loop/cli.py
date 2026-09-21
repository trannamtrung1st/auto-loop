"""Typer CLI entrypoint and command registration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from auto_loop import __version__
from auto_loop.config import ConfigurationError
from auto_loop.exits import ExitCode
from auto_loop.git import GitProtocolError
from auto_loop.doctor import run_doctor
from auto_loop.locking import ConcurrentRunError
from auto_loop.loop import run_lifecycle
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.run_options import build_run_options
from auto_loop.logs_view import render_logs, stream_follow_logs
from auto_loop.run_inputs import RunInputError, RunInputs, goal_summary, prepare_repo_for_run
from auto_loop.run_prerequisites import RunPreconditionError
from auto_loop.status_report import build_status_report
from auto_loop.stop_control import StopError, request_remote_stop
from auto_loop.init_cmd import InitError, run_init
from auto_loop.migrate_cmd import MigrateError, migrate_legacy_layout
from auto_loop.paths import resolve_repository_path

app = typer.Typer(
    name="auto-loop",
    help=(
        "Give Auto Loop a goal, optionally configure it, then let Auto Loop manage "
        "planning, execution, review, and tool-managed state under .auto-loop/.\n\n"
        '  auto-loop init\n'
        '  auto-loop run "Describe what you want to build"\n'
        "  auto-loop status\n"
        "  auto-loop resume"
    ),
    no_args_is_help=True,
    add_completion=False,
)

PathArgument = Annotated[
    Optional[Path],
    typer.Argument(
        help="Target repository path (defaults to current working directory).",
        exists=False,
        file_okay=False,
        dir_okay=True,
        resolve_path=False,
    ),
]


def _resolve_path(path: Path | None) -> Path:
    try:
        return resolve_repository_path(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc


def _exit_config(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))


def _split_goal_and_repo(goal: str | None, path: Path | None) -> tuple[Path, str | None]:
    """Treat an existing directory positional as a legacy repository path."""
    if goal is None:
        return _resolve_path(path), None
    candidate = Path(goal).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = None
    if resolved is not None and resolved.is_dir() and path is None:
        return resolved, None
    if resolved is not None and resolved.is_file() and path is None:
        _exit_config(
            f"That looks like a file: {goal}\n\n"
            f"Use:\n  auto-loop run --goal-file {goal}"
        )
    return _resolve_path(path), goal


@app.callback(invoke_without_command=True)
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show version and exit."),
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("init")
def init_cmd(
    path: PathArgument = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite auto-loop.yaml and regenerate tool-managed templates."),
    ] = False,
    minimal: Annotated[
        bool,
        typer.Option("--minimal", help="Write skeleton config without default instruction files."),
    ] = False,
) -> None:
    """Create user-owned auto-loop.yaml. Does not create a goal or require editing .auto-loop/."""
    repo = _resolve_path(path)
    try:
        result = run_init(repo, force=force, minimal=minimal)
    except InitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    typer.echo(result.message)
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("migrate")
def migrate_cmd(
    path: PathArgument = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite auto-loop.yaml if it already exists.")] = False,
) -> None:
    """Create auto-loop.yaml from a legacy layout without discarding run state."""
    repo = _resolve_path(path)
    try:
        result = migrate_legacy_layout(repo, force=force)
    except MigrateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    typer.echo(result.message)
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("doctor")
def doctor_cmd(
    path: PathArgument = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show passing checks.")] = False,
) -> None:
    """Validate workspace, Git, provider, and configuration."""
    repo = _resolve_path(path)
    report = run_doctor(repo, verbose=verbose)
    typer.echo(report.render(verbose=verbose))
    if not report.ok:
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))
    raise typer.Exit(code=int(ExitCode.COMPLETE))


def _run_with_inputs(
    repo: Path,
    inputs: RunInputs,
    *,
    model: str | None,
    planner_model: str | None,
    worker_model: str | None,
    reviewer_model: str | None,
    max_turns: int | None,
    max_runtime_minutes: int | None,
    verbose: bool,
    quiet: bool,
) -> None:
    try:
        prepared = prepare_repo_for_run(repo, inputs)
        config = prepared.config
        options = build_run_options(
            config,
            model=model,
            planner_model=planner_model,
            worker_model=worker_model,
            reviewer_model=reviewer_model,
            max_turns=max_turns,
            max_runtime_minutes=max_runtime_minutes,
            verbose=verbose,
            quiet=quiet,
            goal_summary=goal_summary(prepared.goal_text),
            user_config_rel=prepared.user_config_rel,
            resuming=prepared.is_resume,
        )
        outcome = run_lifecycle(
            repo,
            options,
            SubprocessCursorProvider(config),
            inputs=inputs,
        )
    except (RunPreconditionError, RunInputError, ConfigurationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    except GitProtocolError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.GIT_PROTOCOL_ERROR)) from exc
    except ConcurrentRunError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.CONCURRENT_RUN)) from exc
    if outcome.message:
        typer.echo(outcome.message)
    elif not quiet:
        typer.echo(f"Lifecycle finished with exit code {int(outcome.exit_code)}")
    raise typer.Exit(code=int(outcome.exit_code))


@app.command("run")
def run_cmd(
    goal: Annotated[
        Optional[str],
        typer.Argument(help='Goal text for a new run, for example "Add keyboard navigation".'),
    ] = None,
    path: Annotated[
        Optional[Path],
        typer.Option("--path", help="Target repository (defaults to the current directory)."),
    ] = None,
    goal_file: Annotated[
        Optional[Path],
        typer.Option("--goal-file", help="Read the run goal from a markdown file."),
    ] = None,
    context: Annotated[
        Optional[Path],
        typer.Option("--context", help="Optional explicit context.yaml to snapshot for this run."),
    ] = None,
    model: Annotated[Optional[str], typer.Option("--model", help="Override all role models.")] = None,
    planner_model: Annotated[Optional[str], typer.Option("--planner-model")] = None,
    worker_model: Annotated[Optional[str], typer.Option("--worker-model")] = None,
    reviewer_model: Annotated[Optional[str], typer.Option("--reviewer-model")] = None,
    max_turns: Annotated[Optional[int], typer.Option("--max-turns", min=1)] = None,
    max_runtime_minutes: Annotated[Optional[int], typer.Option("--max-runtime-minutes", min=1)] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
) -> None:
    """Start a run from a goal, or continue the stored run if no new goal is given."""
    repo, goal_text = _split_goal_and_repo(goal, path)
    inputs = RunInputs(
        goal_text=goal_text,
        goal_file=goal_file,
        context_file=context,
        resume_only=False,
    )
    _run_with_inputs(
        repo,
        inputs,
        model=model,
        planner_model=planner_model,
        worker_model=worker_model,
        reviewer_model=reviewer_model,
        max_turns=max_turns,
        max_runtime_minutes=max_runtime_minutes,
        verbose=verbose,
        quiet=quiet,
    )


@app.command("resume")
def resume_cmd(
    path: PathArgument = None,
    model: Annotated[Optional[str], typer.Option("--model", help="Override all role models.")] = None,
    planner_model: Annotated[Optional[str], typer.Option("--planner-model")] = None,
    worker_model: Annotated[Optional[str], typer.Option("--worker-model")] = None,
    reviewer_model: Annotated[Optional[str], typer.Option("--reviewer-model")] = None,
    max_turns: Annotated[Optional[int], typer.Option("--max-turns", min=1)] = None,
    max_runtime_minutes: Annotated[Optional[int], typer.Option("--max-runtime-minutes", min=1)] = None,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
) -> None:
    """Continue the stored run using its saved goal and frozen configuration."""
    repo = _resolve_path(path)
    inputs = RunInputs(resume_only=True)
    _run_with_inputs(
        repo,
        inputs,
        model=model,
        planner_model=planner_model,
        worker_model=worker_model,
        reviewer_model=reviewer_model,
        max_turns=max_turns,
        max_runtime_minutes=max_runtime_minutes,
        verbose=verbose,
        quiet=quiet,
    )


@app.command("status")
def status_cmd(
    path: PathArgument = None,
) -> None:
    """Summarize the current run without opening tool-managed `.auto-loop/` files."""
    repo = _resolve_path(path)
    try:
        typer.echo(build_status_report(repo))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from exc
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("logs")
def logs_cmd(
    path: PathArgument = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Stream new log bytes until Ctrl+C, or until the lifecycle ends and the log is idle.",
        ),
    ] = False,
    turn: Annotated[Optional[int], typer.Option("--turn", min=1)] = None,
    raw: Annotated[bool, typer.Option("--raw")] = False,
) -> None:
    """View per-turn provider logs."""
    repo = _resolve_path(path)
    try:
        if follow:
            stream_follow_logs(repo, turn=turn, raw=raw)
        else:
            output = render_logs(repo, turn=turn, raw=raw, follow=False)
            if output:
                typer.echo(output)
    except KeyboardInterrupt:
        raise typer.Exit(code=int(ExitCode.COMPLETE))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from exc
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("stop")
def stop_cmd(
    path: PathArgument = None,
) -> None:
    """Gracefully stop the active lifecycle."""
    repo = _resolve_path(path)
    try:
        message = request_remote_stop(repo)
    except StopError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    typer.echo(message)
    raise typer.Exit(code=int(ExitCode.STOPPED))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
