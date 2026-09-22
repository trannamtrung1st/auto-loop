"""Protected file and reviewer mutation enforcement tests."""

import subprocess
from pathlib import Path

import pytest

from tests.repo_utils import frozen_config
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.models import ActivePathTarget
from auto_loop.review_targets import sha256_file
from auto_loop.protection import (
    ProtectionViolationError,
    ReviewMutationError,
    assert_protected_unchanged,
    assert_review_snapshot_unchanged,
    assert_reviewer_product_unchanged,
    capture_product_fingerprint,
    capture_protected_baseline,
    capture_review_snapshot,
)
from auto_loop.config import default_config
from auto_loop.product_state import is_product_tree_clean


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    bootstrap_workspace(repo)
    return repo


def test_protected_file_mutation_detected(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    baseline = capture_protected_baseline(repo, config)
    task = repo / ".ai/auto-loop" / "task.md"
    task.write_text(task.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(ProtectionViolationError) as exc:
        assert_protected_unchanged(repo, config, baseline)
    assert any(v.path.endswith("task.md") for v in exc.value.violations)


def test_auto_loop_plan_change_not_product_mutation(tmp_path: Path):
    repo = _repo(tmp_path)
    before = capture_product_fingerprint(repo)
    plan = repo / ".ai/auto-loop" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nupdate\n", encoding="utf-8")
    after = capture_product_fingerprint(repo)
    assert before == after
    assert is_product_tree_clean(repo)


def test_reviewer_product_edit_detected(tmp_path: Path):
    repo = _repo(tmp_path)
    before = capture_product_fingerprint(repo)
    (repo / "product.py").write_text("print('x')\n", encoding="utf-8")
    after = capture_product_fingerprint(repo)
    with pytest.raises(ReviewMutationError) as exc:
        assert_reviewer_product_unchanged(before, after)
    assert exc.value.details


def test_ignored_build_artifact_can_be_simulated_as_untracked_outside_auto_loop(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "gitignore")
    before = capture_product_fingerprint(repo)
    cache = repo / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"123")
    after = capture_product_fingerprint(repo)
    assert before.head == after.head


def test_review_snapshot_detects_plan_mutation(tmp_path: Path):
    repo = _repo(tmp_path)
    plan = repo / ".ai/auto-loop" / "plan.md"
    before = capture_review_snapshot(repo, plan_path=plan)
    plan.write_text(plan.read_text(encoding="utf-8") + "\nreviewer edit\n", encoding="utf-8")
    with pytest.raises(ReviewMutationError, match="plan.md"):
        assert_review_snapshot_unchanged(repo, plan_path=plan, before=before)


def test_reviewer_product_edit_detected_without_git(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    config = default_config()
    config.git.mode = "off"
    (repo / "app.py").write_text("ok\n", encoding="utf-8")
    before = capture_product_fingerprint(repo, config=config)
    (repo / "app.py").write_text("mutated\n", encoding="utf-8")
    after = capture_product_fingerprint(repo, config=config)
    with pytest.raises(ReviewMutationError):
        assert_reviewer_product_unchanged(before, after)


def test_review_snapshot_detects_path_target_mutation(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore")
    build = repo / "build"
    build.mkdir()
    report = build / "report.html"
    report.write_text("ok\n", encoding="utf-8")
    target = ActivePathTarget(
        id="generated",
        path="build/report.html",
        fingerprint=sha256_file(report),
        exists=True,
        git_classification="ignored",
    )
    plan = repo / ".ai/auto-loop" / "plan.md"
    before = capture_review_snapshot(repo, plan_path=plan, targets=[target])
    report.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(ReviewMutationError, match="path target"):
        assert_review_snapshot_unchanged(repo, plan_path=plan, before=before, targets=[target])
