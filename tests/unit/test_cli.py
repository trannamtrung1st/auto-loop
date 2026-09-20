"""CLI registration and PATH argument tests."""

from pathlib import Path

from typer.testing import CliRunner

from auto_loop.cli import app

runner = CliRunner()


def test_help_exposes_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("init", "doctor", "run", "status", "logs", "stop"):
        assert name in result.stdout
    assert "resources" in result.stdout


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


def test_resources_install_subgroup(tmp_path: Path):
    result = runner.invoke(app, ["resources", "install", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "CREATE AGENTS.md" in result.stdout
    assert "CREATE .agents/skills/repo-discovery/SKILL.md" in result.stdout
    assert "ui-validation" not in result.stdout


def test_resources_install_honors_path_argument(tmp_path: Path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    result = runner.invoke(
        app,
        ["resources", "install", str(target), "--dry-run", "--no-agents-md"],
    )
    assert result.exit_code == 0
    assert "SKIP AGENTS.md" in result.stdout
    assert not (other / "AGENTS.md").exists()


def test_invalid_repository_path_exits_config_error(tmp_path: Path):
    missing = tmp_path / "missing"
    result = runner.invoke(app, ["status", str(missing)])
    assert result.exit_code == 10
