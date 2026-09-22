"""Product working-tree fingerprint tests for planner mutation detection."""

from pathlib import Path

from auto_loop.product_state import (
    capture_product_working_fingerprint,
    product_working_fingerprints_equal,
)
from tests.integration.scenario_harness import git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init")
    return repo


def test_fingerprint_detects_content_change_with_same_git_status(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    before_head, before_rows = capture_product_working_fingerprint(repo)
    (repo / "README.md").write_text("dirty again\n", encoding="utf-8")
    after_head, after_rows = capture_product_working_fingerprint(repo)
    assert before_head == after_head
    assert before_rows[0][1] == after_rows[0][1]
    assert not product_working_fingerprints_equal(before_rows, after_rows)
