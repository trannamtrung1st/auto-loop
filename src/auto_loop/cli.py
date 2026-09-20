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
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import build_run_options
from auto_loop.run_prerequisites import RunPreconditionError
from auto_loop.init_cmd import InitError, run_init
from auto_loop.paths import resolve_repository_path
from auto_loop.resources_install import ResourcesInstallError, run_resources_install

app = typer.Typer(
    name="auto-loop",
    help="Two-role autonomous implementation/review loop.",
    no_args_is_help=True,
    add_completion=False,
)

resources_app = typer.Typer(help="Install optional shared AI-harness resources.")
app.add_typer(resources_app, name="resources")

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
    model: Annotated[Optional[str], typer.Option("--model", help="Override both role models.")] = None,
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
            worker_model=worker_model,
            reviewer_model=reviewer_model,
            max_turns=max_turns,
            max_runtime_minutes=max_runtime_minutes,
            verbose=verbose,
            quiet=quiet,
        )
        outcome = run_lifecycle(repo, options, ScriptedProvider())
    except (RunPreconditionError, ConfigurationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(exc.exit_code)) from exc
    except GitProtocolError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.GIT_PROTOCOL_ERROR)) from exc
    if not quiet:
        typer.echo(f"Lifecycle finished with exit code {int(outcome.exit_code)}")
    raise typer.Exit(code=int(outcome.exit_code))


@app.command("status")
def status_cmd(
    path: PathArgument = None,
) -> None:
    """Show lifecycle health and progress."""
    _resolve_path(path)
    typer.echo("status: not yet implemented", err=True)
    raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR))


@app.command("logs")
def logs_cmd(
    path: PathArgument = None,
    follow: Annotated[bool, typer.Option("--follow")] = False,
    turn: Annotated[Optional[int], typer.Option("--turn", min=1)] = None,
    raw: Annotated[bool, typer.Option("--raw")] = False,
) -> None:
    """View per-turn provider logs."""
    _resolve_path(path)
    typer.echo("logs: not yet implemented", err=True)
    raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR))


@app.command("stop")
def stop_cmd(
    path: PathArgument = None,
) -> None:
    """Gracefully stop the active lifecycle."""
    _resolve_path(path)
    typer.echo("stop: not yet implemented", err=True)
    raise typer.Exit(code=int(ExitCode.INTERNAL_ERROR))


@resources_app.command("install")
def resources_install_cmd(
    path: PathArgument = None,
    profile: Annotated[
        str,
        typer.Option("--profile", help="Resource profile: core or frontend."),
    ] = "core",
    no_agents_md: Annotated[bool, typer.Option("--no-agents-md")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Install optional AGENTS.md and .agents/skills resources."""
    repo = _resolve_path(path)
    try:
        result = run_resources_install(
            repo,
            profile=profile,
            no_agents_md=no_agents_md,
            dry_run=dry_run,
        )
    except ResourcesInstallError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.CONFIG_ERROR)) from exc
    for line in result.lines():
        typer.echo(line)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
