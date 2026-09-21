"""CLI registration and PATH argument tests."""

from pathlib import Path

from rich.text import Text
from typer.testing import CliRunner

from auto_loop.cli import app

runner = CliRunner()


def test_help_exposes_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("init", "doctor", "run", "status", "logs", "stop"):
        assert name in result.stdout
    assert "resources" not in result.stdout


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


def test_init_accepts_path_argument(tmp_path: Path):
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0


def test_doctor_accepts_path_argument(tmp_path: Path):
    result = runner.invoke(app, ["doctor", str(tmp_path)])
    assert result.exit_code == 10


def test_run_accepts_path_and_flags(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path),
            "--max-turns",
            "5",
            "--verbose",
        ],
    )
    assert result.exit_code == 10


def test_run_help_lists_model_and_limit_flags():
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
        "--verbose",
        "--quiet",
    ):
        assert flag in plain_output


def test_run_minimal_init_fails_closed_before_provider(tmp_path: Path):
    repo = tmp_path / "minimal"
    repo.mkdir()
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    from auto_loop.init_cmd import run_init

    run_init(repo, minimal=True)
    result = runner.invoke(app, ["run", str(repo)])
    assert result.exit_code == 10
    assert "Missing instruction templates" in result.stderr or "Missing instruction templates" in result.stdout


def test_resources_command_removed():
    result = runner.invoke(app, ["resources", "install"])
    assert result.exit_code != 0


def test_status_reports_idle_workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    from auto_loop.init_cmd import run_init

    run_init(repo, minimal=True)
    result = runner.invoke(app, ["status", str(repo)])
    assert result.exit_code == 0
    assert "status: idle" in result.stdout or "status: running" in result.stdout


def test_logs_cli_accepts_path_and_turn(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    from auto_loop.init_cmd import run_init

    run_init(repo, minimal=True)
    result = runner.invoke(app, ["logs", str(repo), "--turn", "1"])
    assert result.exit_code == 0


def test_invalid_repository_path_exits_config_error(tmp_path: Path):
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["status", str(missing)])
    assert result.exit_code == 10
