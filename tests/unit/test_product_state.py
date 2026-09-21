"""Product state classification tests."""

import subprocess
from pathlib import Path

from auto_loop.product_state import is_control_path, list_product_changes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tracked.txt").write_text("t", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    return repo


def test_is_control_path():
    assert is_control_path(".auto-loop/plan.md")
    assert is_control_path(".auto-loop/runtime/state.json")
    assert is_control_path("auto-loop.yaml")
    assert not is_control_path("src/main.py")


def test_staged_product_change_detected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "tracked.txt").write_text("changed", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    changes = list_product_changes(repo)
    assert any(c.path == "tracked.txt" for c in changes)
