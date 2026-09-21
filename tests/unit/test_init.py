"""Init command and packaged template tests."""

import subprocess
from importlib import resources
from pathlib import Path

import pytest
import yaml

from auto_loop.config import load_config
from auto_loop.init_cmd import InitError, run_init
from auto_loop.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    return repo


def test_default_init_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    result = run_init(repo)
    root = repo / ".auto-loop"
    assert (root / "config.yaml").is_file()
    assert (root / "context.yaml").is_file()
    assert (root / "task.md").is_file()
    assert (root / "plan.md").is_file()
    assert (root / "agents" / "planner.md").is_file()
    assert (root / "agents" / "worker.md").is_file()
    assert (root / "instructions" / "planner.md").is_file()
    assert (root / "instructions" / "shared.md").is_file()
    assert (root / "reviews").is_dir()
    assert (root / "resources").is_dir()
    assert (root / "runtime").is_dir()
    assert result.created


def test_repeated_init_non_destructive(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    before = (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")
    run_init(repo)
    after = (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")
    assert before == after


def test_minimal_init_skips_instructions_and_resources(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo, minimal=True)
    root = repo / ".auto-loop"
    assert not (root / "instructions").exists()
    assert not (root / "resources").exists()
    cfg = load_config(root / "config.yaml")
    assert cfg.instructions.worker.files == []


def test_minimal_refuses_nonempty_task_without_force(tmp_path: Path):
    repo = _repo(tmp_path)
    task = repo / ".auto-loop" / "task.md"
    task.parent.mkdir(parents=True)
    task.write_text("existing task\n", encoding="utf-8")
    with pytest.raises(InitError):
        run_init(repo, minimal=True)


def test_force_regenerates_control_templates(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    worker = repo / ".auto-loop" / "agents" / "worker.md"
    worker.write_text("custom\n", encoding="utf-8")
    run_init(repo, force=True)
    assert "implementation worker" in worker.read_text(encoding="utf-8").lower() or "implementation" in worker.read_text(encoding="utf-8").lower()


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


def test_init_cli_path_argument(tmp_path: Path):
    repo = _repo(tmp_path)
    result = runner.invoke(app, ["init", str(repo)])
    assert result.exit_code == 0
    assert (repo / ".auto-loop" / "config.yaml").exists()


def test_context_yaml_parseable(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    data = yaml.safe_load((repo / ".auto-loop" / "context.yaml").read_text(encoding="utf-8"))
    assert data["version"] == 1
