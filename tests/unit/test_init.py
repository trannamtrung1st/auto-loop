"""Init command and packaged template tests."""

import subprocess
from importlib import resources
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.config import load_config_from_repo, user_config_path
from auto_loop.init_cmd import InitError, bootstrap_workspace, run_init

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


def test_default_init_creates_user_config_only(tmp_path: Path):
    repo = _repo(tmp_path)
    result = run_init(repo)
    assert (repo / "auto-loop.yaml").is_file()
    assert not (repo / "task.md").exists()
    assert not (repo / "context.yaml").exists()
    assert not (repo / ".auto-loop" / "task.md").exists()
    assert not (repo / ".auto-loop" / "config.yaml").exists()
    assert "auto-loop run" in result.message
    assert result.created
    assert ".auto-loop/runtime/" in result.message
    assert not (repo / ".gitignore").exists()


def test_repeated_init_non_destructive(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    before = user_config_path(repo).read_text(encoding="utf-8")
    run_init(repo)
    after = user_config_path(repo).read_text(encoding="utf-8")
    assert before == after


def test_minimal_init_writes_empty_instruction_files(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo, minimal=True)
    cfg = load_config_from_repo(repo)
    assert cfg.instructions.worker.files == []
    assert not (repo / ".auto-loop" / "instructions").exists()


def test_minimal_does_not_create_root_task(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo, minimal=True)
    assert not (repo / "task.md").exists()
    assert not (repo / ".auto-loop" / "task.md").exists()


def test_force_regenerates_control_templates_when_workspace_exists(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    worker = repo / ".auto-loop" / "agents" / "worker.md"
    worker.write_text("custom\n", encoding="utf-8")
    run_init(repo, force=True)
    text = worker.read_text(encoding="utf-8").lower()
    assert "implementation worker" in text or "implementation" in text


def test_force_refuses_while_resumable_run_exists(tmp_path: Path):
    repo = _repo(tmp_path)
    _git(repo, "commit", "--allow-empty", "-m", "init")
    bootstrap_workspace(repo, goal="In progress")
    from auto_loop.git import head_commit
    from auto_loop.lifecycle import create_lifecycle
    from auto_loop.runtime import save_lifecycle_state

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    with pytest.raises(InitError, match="in progress"):
        run_init(repo, force=True)


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
    assert (repo / "auto-loop.yaml").exists()
    assert "Next:" in result.stdout
    assert "auto-loop run" in result.stdout
    assert not (repo / "task.md").exists()
    assert not (repo / "context.yaml").exists()


def test_bootstrap_writes_internal_context(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    data = yaml.safe_load((repo / ".auto-loop" / "context.yaml").read_text(encoding="utf-8"))
    assert data["version"] == 1
    cfg = load_config_from_repo(repo)
    assert cfg.task_file == ".auto-loop/task.md"
