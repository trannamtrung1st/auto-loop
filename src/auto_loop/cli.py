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
from auto_loop.manifest import load_run_manifest, resolve_operational_source
from auto_loop.run_inputs import RunInputError, goal_summary, prepare_repo_for_run
from auto_loop.run_prerequisites import RunPreconditionError
from auto_loop.status_report import build_status_report
from auto_loop.stop_control import StopError, request_remote_stop
from auto_loop.init_cmd import InitError, run_init

app = typer.Typer(
    name="auto-loop",
    help=(
        "Run Auto Loop from one explicit run manifest and one task document.\n\n"
        "  auto-loop run .ai/run.yaml\n"
        "  auto-loop status .ai/run.yaml\n"
        "  auto-loop resume .ai/run.yaml"
    ),
    no_args_is_help=True,
    add_completion=False,
)

ConfigArgument = Annotated[
    Path,
    typer.Argument(
        help="Run config YAML (any filename or location).",
        exists=False,
        file_okay=True,
        dir_okay=False,
        resolve_path=False,
    ),
]


def _exit_config(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))


def _load_manifest(run_config: Path):
    try:
        return load_run_manifest(run_config)
    except ConfigurationError as exc:
        _exit_config(str(exc))


def _resolve_operational(run_config: Path):
    try:
        return resolve_operational_source(run_config)
    except ConfigurationError as exc:
        _exit_config(str(exc))


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
    run_config: Annotated[
        Path,
        typer.Argument(help="Path for the starter run YAML to create."),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite the target YAML if it already exists."),
    ] = False,
) -> None:
    """Write a starter v2 run YAML. Does not create runtime state or a task file."""
    try:
        result = run_init(run_config, force=force)
    except InitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    typer.echo(result.message)
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("doctor")
def doctor_cmd(
    run_config: ConfigArgument,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show passing checks.")] = False,
) -> None:
    """Validate the run manifest, workspace, Git, provider, and current artifacts."""
    try:
        source = load_run_manifest(run_config)
    except ConfigurationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc
    report = run_doctor(source, verbose=verbose)
    typer.echo(report.render(verbose=verbose))
    if not report.ok:
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR))
    raise typer.Exit(code=int(ExitCode.COMPLETE))


def _run_prepared(
    source,
    *,
    resume: bool,
    verbose: bool,
    quiet: bool,
) -> None:
    try:
        prepared = prepare_repo_for_run(source, resume=resume)
        options = build_run_options(
            prepared.config,
            verbose=verbose,
            quiet=quiet,
            goal_summary=goal_summary(prepared.goal_text),
            user_config_rel=prepared.user_config_rel,
            resuming=prepared.is_resume,
        )
        outcome = run_lifecycle(
            prepared.workspace,
            options,
            SubprocessCursorProvider(prepared.config),
            config=prepared.config,
            artifact_root=prepared.artifact_root,
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
    run_config: ConfigArgument,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
) -> None:
    """Start a new lifecycle from the run manifest. Refuses if a run is already active."""
    source = _load_manifest(run_config)
    _run_prepared(source, resume=False, verbose=verbose, quiet=quiet)


@app.command("resume")
def resume_cmd(
    run_config: ConfigArgument,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
    quiet: Annotated[bool, typer.Option("--quiet")] = False,
) -> None:
    """Continue the stored run using its frozen configuration and task snapshot."""
    source = _load_manifest(run_config)
    _run_prepared(source, resume=True, verbose=verbose, quiet=quiet)


@app.command("status")
def status_cmd(
    run_config: ConfigArgument,
) -> None:
    """Summarize the current run for the workspace located by the manifest."""
    source = _resolve_operational(run_config)
    try:
        typer.echo(build_status_report(source))
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR)) from exc
    raise typer.Exit(code=int(ExitCode.COMPLETE))


@app.command("logs")
def logs_cmd(
    run_config: ConfigArgument,
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
    source = _resolve_operational(run_config)
    try:
        if follow:
            stream_follow_logs(
                source.workspace,
                turn=turn,
                raw=raw,
                artifact_root=source.artifact_root,
            )
        else:
            output = render_logs(
                source.workspace,
                turn=turn,
                raw=raw,
                follow=False,
                artifact_root=source.artifact_root,
            )
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
    run_config: ConfigArgument,
) -> None:
    """Gracefully stop the active lifecycle located by the manifest."""
    source = _resolve_operational(run_config)
    try:
        message = request_remote_stop(source.workspace, artifact_root=source.artifact_root)
    except StopError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    typer.echo(message)
    raise typer.Exit(code=int(ExitCode.STOPPED))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
