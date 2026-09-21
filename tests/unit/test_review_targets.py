"""Review-target path normalization and fingerprint tests."""

from pathlib import Path

import pytest

from auto_loop.git import GitProtocolError, head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.models import PathTargetRequest
from auto_loop.review_targets import (
    classify_path,
    fingerprint_path,
    normalize_path_target,
    normalize_work_targets,
    resolve_review_path,
    sha256_bytes,
)
from tests.integration.scenario_harness import git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "commit", "--allow-empty", "-m", "init")
    bootstrap_workspace(repo)
    return repo


def test_path_traversal_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    with pytest.raises(GitProtocolError, match="escapes workspace"):
        resolve_review_path(repo, "../outside")


def test_file_and_directory_fingerprints(tmp_path: Path):
    repo = _repo(tmp_path)
    file_path = repo / "note.txt"
    file_path.write_text("hello\n", encoding="utf-8")
    digest, exists = fingerprint_path(file_path)
    assert exists is True
    assert digest == sha256_bytes(b"hello\n")
    folder = repo / "pkg"
    folder.mkdir()
    (folder / "a.py").write_text("a\n", encoding="utf-8")
    dir_digest, dir_exists = fingerprint_path(folder)
    assert dir_exists is True
    assert dir_digest != digest
    missing, missing_exists = fingerprint_path(repo / "nope.txt")
    assert missing_exists is False
    assert missing == sha256_bytes(b"")


def test_ignored_tracked_untracked_classification(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore")
    (repo / "tracked.txt").write_text("t\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "tracked")
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("x", encoding="utf-8")
    assert classify_path(repo, "tracked.txt") == "tracked"
    assert classify_path(repo, "untracked.txt") == "untracked"
    assert classify_path(repo, "build/out.bin") == "ignored"
    assert classify_path(repo, ".auto-loop/plan.md") == "control"


def test_untracked_path_target_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "loose.py").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitProtocolError, match="untracked"):
        normalize_path_target(
            repo,
            PathTargetRequest(id="loose", path="loose.py"),
            scope="batch",
        )


def test_empty_git_range_with_path_target_accepted(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore")
    (repo / "build").mkdir()
    (repo / "build" / "report.html").write_text("ok\n", encoding="utf-8")
    from auto_loop.models import ReviewRequest

    head = head_commit(repo)
    targets, warnings = normalize_work_targets(
        repo,
        last_approved_commit=head,
        request=ReviewRequest(
            scope="batch",
            target="generated",
            summary="path only",
            targets=[PathTargetRequest(id="generated", path="build/report.html")],
        ),
    )
    assert warnings == ()
    assert len(targets) == 1
    assert targets[0].kind == "path"
    assert targets[0].git_classification == "ignored"


def test_empty_git_range_without_target_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.models import ReviewRequest

    head = head_commit(repo)
    with pytest.raises(GitProtocolError, match="empty"):
        normalize_work_targets(
            repo,
            last_approved_commit=head,
            request=ReviewRequest(scope="batch", target="W01", summary="empty"),
        )
