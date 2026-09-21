"""Typer CLI entrypoint and command registration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from auto_loop import __version__
from auto_loop.config import ConfigurationError, load_config_from_repo
from auto_loop.exits import ExitCode
from auto_loop.git import GitProtocolError
from auto_loop.doctor import run_doctor
from auto_loop.locking import ConcurrentRunError
from auto_loop.loop import run_lifecycle
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.run_options import build_run_options
from auto_loop.logs_view import render_logs, stream_follow_logs
from auto_loop.run_prerequisites import RunPreconditionError
from auto_loop.status_report import build_status_report
from auto_loop.stop_control import StopError, request_remote_stop
from auto_loop.init_cmd import InitError, run_init
from auto_loop.paths import resolve_repository_path

app = typer.Typer(
    name="auto-loop",
    help="Planner/worker/reviewer autonomous implementation/review loop.",
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
    force: Annotated[bool, typer.Option("--force", help="Regenerate generated control templates.")] = False,
    minimal: Annotated[bool, typer.Option("--minimal", help="Create minimal control skeleton only.")] = False,
) -> None:
    """Create .auto-loop control workspace templates."""
    repo = _resolve_path(path)
    try:
        result = run_init(repo, force=force, minimal=minimal)
    except InitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    for rel in result.created:
        typer.echo(f"created {rel}")
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


@app.command("run")
def run_cmd(
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
    """Start or continue the implementation/review lifecycle."""
    repo = _resolve_path(path)
    try:
        config = load_config_from_repo(repo)
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
        )
        outcome = run_lifecycle(repo, options, SubprocessCursorProvider(config))
    except (RunPreconditionError, ConfigurationError) as exc:
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


@app.command("status")
def status_cmd(
    path: PathArgument = None,
) -> None:
    """Show lifecycle health and progress."""
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
