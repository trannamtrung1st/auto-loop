"""Git range and repository helper tests."""

import subprocess
from pathlib import Path

import pytest

from auto_loop.git import (
    GitProtocolError,
    ReviewRequestError,
    assert_approved_baseline_ancestry,
    format_git_protocol_error,
    git_has_commits,
    head_commit,
    head_commit_optional,
    is_ancestor,
    normalize_batch_range,
)
from auto_loop.product_state import assert_clean_product_tree, is_product_tree_clean, list_product_changes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_unborn_repository_has_no_optional_head(tmp_path: Path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    assert not git_has_commits(repo)
    assert head_commit_optional(repo) is None
    with pytest.raises(GitProtocolError, match="no commits"):
        head_commit(repo)


def test_product_tree_allows_auto_loop_changes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    control = repo / ".ai/auto-loop"
    control.mkdir(parents=True)
    (control / "plan.md").write_text("plan\n", encoding="utf-8")
    assert is_product_tree_clean(repo)
    assert list_product_changes(repo) == []


def test_product_tree_detects_untracked_product_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "new.py").write_text("x", encoding="utf-8")
    changes = list_product_changes(repo)
    assert len(changes) == 1
    assert changes[0].path == "new.py"
    with pytest.raises(GitProtocolError, match="not clean"):
        assert_clean_product_tree(repo)


def test_normalize_batch_range_exact(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    (repo / "feature.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    head = head_commit(repo)
    normalized = normalize_batch_range(repo, last_approved_commit=base)
    assert normalized.range.base == base
    assert normalized.range.head == head
    assert normalized.range.as_revision_range() == f"{base}..{head}"


def test_wrong_worker_base_warns_and_normalizes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "a")
    mid = head_commit(repo)
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "b")
    normalized = normalize_batch_range(
        repo,
        last_approved_commit=base,
        worker_base_commit=mid,
        worker_head_commit=head_commit(repo),
    )
    assert normalized.range.base == base
    assert normalized.warnings


def test_dirty_tree_blocks_batch_normalization(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    (repo / "dirty.txt").write_text("d", encoding="utf-8")
    with pytest.raises(GitProtocolError, match="not clean"):
        normalize_batch_range(repo, last_approved_commit=base)


def test_wrong_worker_head_raises_review_request_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    (repo / "feature.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    with pytest.raises(ReviewRequestError, match="head_commit does not match"):
        normalize_batch_range(
            repo,
            last_approved_commit=base,
            worker_head_commit=base,
            require_clean=False,
        )


def test_unknown_worker_head_ref_raises_review_request_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    with pytest.raises(ReviewRequestError, match="Worker head_commit"):
        normalize_batch_range(
            repo,
            last_approved_commit=base,
            worker_head_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            require_clean=False,
            allow_empty=True,
        )


def test_format_git_protocol_error_names_invariant_shas_and_preserved_state(tmp_path: Path):
    repo = _init_repo(tmp_path)
    approved = head_commit(repo)
    (repo / "x.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-m", "x")
    _git(repo, "checkout", "--orphan", "rewritten")
    (repo / "README.md").write_text("rewritten\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "new root")
    head = head_commit(repo)
    with pytest.raises(GitProtocolError, match="history rewrite") as caught:
        assert_approved_baseline_ancestry(repo, approved)
    text = format_git_protocol_error(caught.value)
    assert text.splitlines()[0] == "Git protocol error"
    assert "Invariant: Approved baseline is no longer an ancestor of HEAD" in text
    assert f"Approved: {approved}" in text
    assert f"HEAD: {head}" in text
    assert "State: preserved" in text


def test_dirty_tree_protocol_error_names_paths(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "dirty.txt").write_text("d", encoding="utf-8")
    with pytest.raises(GitProtocolError, match="not clean") as caught:
        assert_clean_product_tree(repo)
    text = format_git_protocol_error(caught.value)
    assert "Paths: dirty.txt" in text
    assert "State: preserved" in text


def test_history_rewrite_raises_git_protocol_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    approved = head_commit(repo)
    (repo / "x.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-m", "x")
    _git(repo, "checkout", "--orphan", "rewritten")
    (repo / "README.md").write_text("rewritten\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "new root")
    with pytest.raises(GitProtocolError, match="history rewrite"):
        assert_approved_baseline_ancestry(repo, approved)


def test_empty_range_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head = head_commit(repo)
    with pytest.raises(GitProtocolError, match="empty"):
        normalize_batch_range(repo, last_approved_commit=head)


def test_empty_range_allowed_when_requested(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head = head_commit(repo)
    normalized = normalize_batch_range(repo, last_approved_commit=head, allow_empty=True)
    assert normalized.range.base == head
    assert normalized.range.head == head


def test_amended_review_fix_head_still_normalizes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    approved = head_commit(repo)
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "production")
    (repo / "c.txt").write_text("c1\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-m", "review-fix")
    (repo / "c.txt").write_text("c2\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "--amend", "-m", "review-fix amended")
    amended = head_commit(repo)
    normalized = normalize_batch_range(
        repo,
        last_approved_commit=approved,
        worker_base_commit=approved,
        worker_head_commit=amended,
    )
    assert normalized.range.head == amended
    assert is_ancestor(repo, approved, amended)


def test_ancestor_check(tmp_path: Path):
    repo = _init_repo(tmp_path)
    base = head_commit(repo)
    (repo / "c.txt").write_text("c", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-m", "c")
    head = head_commit(repo)
    assert is_ancestor(repo, base, head)
    assert not is_ancestor(repo, head, base)
