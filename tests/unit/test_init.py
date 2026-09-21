"""Init command generates a starter v2 run YAML only."""

from importlib import resources
from pathlib import Path

from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.init_cmd import bootstrap_workspace, run_init
from auto_loop.manifest import load_run_manifest

runner = CliRunner()


def test_default_init_creates_run_yaml_only(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".ai" / "run.yaml"
    result = run_init(target)
    assert target.is_file()
    assert "version: 2" in target.read_text(encoding="utf-8")
    assert not (tmp_path / "task.md").exists()
    assert not (tmp_path / "proposal.md").exists()
    assert not (tmp_path / ".ai" / "auto-loop" / "task.md").exists()
    assert "auto-loop run" in result.message
    assert "proposal.md" in result.message
    assert result.created


def test_repeated_init_non_destructive(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".ai" / "run.yaml"
    run_init(target)
    before = target.read_text(encoding="utf-8")
    run_init(target)
    after = target.read_text(encoding="utf-8")
    assert before == after


def test_force_overwrites_starter_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".ai" / "run.yaml"
    run_init(target)
    target.write_text("stale\n", encoding="utf-8")
    run_init(target, force=True)
    assert "version: 2" in target.read_text(encoding="utf-8")


def test_packaged_worker_reviewer_contract_clauses():
    pkg = resources.files("auto_loop").joinpath("templates")
    planner = pkg.joinpath("agents/planner.md").read_text(encoding="utf-8")
    worker = pkg.joinpath("agents/worker.md").read_text(encoding="utf-8")
    reviewer = pkg.joinpath("agents/reviewer.md").read_text(encoding="utf-8")
    assert "you do not implement product changes" in planner.lower()
    assert "first execution turn" in worker.lower()
    assert "never declare the overall task complete" in worker.lower()
    assert "complete" in worker.lower() and "pass" in worker.lower()
    assert "last_approved_commit..head" in worker.lower()
    assert "planning session purpose" in reviewer.lower()
    assert "execution session purpose" in reviewer.lower()
    assert "sole completion authority" in reviewer.lower()
    assert "whole-task" in reviewer.lower() or "whole-task review" in reviewer.lower()


def test_init_cli_path_argument(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".ai" / "run.yaml"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0
    assert target.exists()
    assert "Next:" in result.stdout
    assert "auto-loop run" in result.stdout
    assert not (tmp_path / "task.md").exists()


def test_bootstrap_writes_manifest_and_task_snapshot(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_workspace(repo, goal="Bootstrapped goal")
    source = load_run_manifest(repo / ".ai" / "run.yaml")
    assert source.workspace.resolve() == repo.resolve()
    assert (repo / ".ai" / "proposal.md").is_file()
    assert (source.artifact_root / "task.md").is_file()
    assert "Bootstrapped goal" in (source.artifact_root / "task.md").read_text(encoding="utf-8")
    assert source.config.task_file == ".ai/auto-loop/task.md"
    assert not (source.artifact_root / "agents").exists()
