"""CLI registration and explicit run-config argument tests."""

from pathlib import Path

from rich.text import Text
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.exits import ExitCode
from tests.repo_utils import git_repo

runner = CliRunner()


def test_help_exposes_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "init",
        "doctor",
        "run",
        "status",
        "logs",
        "stop",
        "resume",
        "reconcile-history",
    ):
        assert name in result.stdout
    assert "migrate" not in result.stdout
    assert "resources" not in result.stdout


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_init_accepts_yaml_path(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", str(tmp_path / ".ai" / "run.yaml")])
    assert result.exit_code == 0
    assert (tmp_path / ".ai" / "run.yaml").is_file()


def test_doctor_requires_run_config(tmp_path: Path):
    result = runner.invoke(app, ["doctor", str(tmp_path / "missing.yaml")])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)


def test_run_without_manifest_shows_usage():
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0


def test_run_help_does_not_duplicate_config_flags():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    plain_output = Text.from_ansi(result.stdout).plain
    for flag in (
        "--model",
        "--planner-model",
        "--worker-model",
        "--reviewer-model",
        "--max-turns",
        "--max-runtime-minutes",
        "--goal-file",
        "--context",
        "--path",
    ):
        assert flag not in plain_output
    assert "--verbose" in plain_output
    assert "--quiet" in plain_output


def test_resources_command_removed():
    result = runner.invoke(app, ["resources", "install"])
    assert result.exit_code != 0


def test_status_reports_idle_workspace(tmp_path: Path, monkeypatch):
    repo = git_repo(tmp_path)
    monkeypatch.chdir(repo)
    yaml_path = repo / ".ai" / "run.yaml"
    runner.invoke(app, ["init", str(yaml_path)])
    (repo / ".ai" / "proposal.md").write_text("Idle goal\n", encoding="utf-8")
    result = runner.invoke(app, ["status", str(yaml_path)])
    assert result.exit_code == 0
    assert "status: idle" in result.stdout or "status: running" in result.stdout


def test_logs_cli_accepts_config_and_turn(tmp_path: Path, monkeypatch):
    repo = git_repo(tmp_path)
    monkeypatch.chdir(repo)
    yaml_path = repo / ".ai" / "run.yaml"
    runner.invoke(app, ["init", str(yaml_path)])
    (repo / ".ai" / "proposal.md").write_text("Logs goal\n", encoding="utf-8")
    result = runner.invoke(app, ["logs", str(yaml_path), "--turn", "1"])
    assert result.exit_code == 0


def test_invalid_config_path_exits_config_error(tmp_path: Path):
    missing = tmp_path / "missing.yaml"
    result = runner.invoke(app, ["status", str(missing)])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
