"""Git repository helpers and batch review range validation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from auto_loop.exits import ExitCode


class GitError(Exception):
    """Git command or repository discovery failure."""


class GitProtocolError(GitError):
    """Review range or product-state invariant violation."""

    exit_code = ExitCode.GIT_PROTOCOL_ERROR


@dataclass(frozen=True)
class ReviewRange:
    base: str
    head: str

    def as_revision_range(self) -> str:
        return f"{self.base}..{self.head}"


@dataclass(frozen=True)
class NormalizedBatchRange:
    range: ReviewRange
    warnings: tuple[str, ...] = ()


def _run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise GitError(stderr or f"git {' '.join(args)} failed")
    return (result.stdout or "").strip()


def is_git_repository(repo: Path) -> bool:
    try:
        _run_git(repo, "rev-parse", "--git-dir")
        return True
    except GitError:
        return False


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return _run_git(repo, "rev-parse", ref)
    except GitError as exc:
        raise GitProtocolError(f"Unknown Git ref: {ref}") from exc


def head_commit(repo: Path) -> str:
    return resolve_commit(repo, "HEAD")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    ancestor_sha = resolve_commit(repo, ancestor)
    descendant_sha = resolve_commit(repo, descendant)
    code = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        cwd=repo,
        capture_output=True,
    ).returncode
    if code == 0:
        return True
    if code == 1:
        return False
    raise GitError("git merge-base --is-ancestor failed")


def assert_approved_baseline_ancestry(repo: Path, last_approved_commit: str) -> None:
    head = head_commit(repo)
    approved = resolve_commit(repo, last_approved_commit)
    if not is_ancestor(repo, approved, head):
        raise GitProtocolError(
            "Approved baseline is no longer an ancestor of HEAD (history rewrite detected)"
        )


def normalize_batch_range(
    repo: Path,
    *,
    last_approved_commit: str,
    worker_base_commit: str | None = None,
    worker_head_commit: str | None = None,
    allow_empty: bool = False,
    excludes: tuple[str, ...] | None = None,
) -> NormalizedBatchRange:
    """Normalize worker batch review to authoritative last_approved..HEAD."""
    from auto_loop.product_state import DEFAULT_PRODUCT_EXCLUDES, assert_clean_product_tree

    assert_approved_baseline_ancestry(repo, last_approved_commit)
    assert_clean_product_tree(repo, excludes=excludes or DEFAULT_PRODUCT_EXCLUDES)

    authoritative_base = resolve_commit(repo, last_approved_commit)
    authoritative_head = head_commit(repo)

    warnings: list[str] = []
    if worker_base_commit:
        worker_base = resolve_commit(repo, worker_base_commit)
        if worker_base != authoritative_base:
            warnings.append(
                "Worker base_commit "
                f"{worker_base[:7]} does not match last_approved_commit "
                f"{authoritative_base[:7]}; using approved baseline"
            )
    if worker_head_commit:
        worker_head = resolve_commit(repo, worker_head_commit)
        if worker_head != authoritative_head:
            raise GitProtocolError(
                "Worker head_commit does not match current HEAD; "
                "commit or reconcile before requesting review"
            )

    if authoritative_base == authoritative_head and not allow_empty:
        raise GitProtocolError("Batch review range is empty (base equals HEAD)")

    if not is_ancestor(repo, authoritative_base, authoritative_head):
        raise GitProtocolError("Batch base is not an ancestor of HEAD")

    return NormalizedBatchRange(
        range=ReviewRange(base=authoritative_base, head=authoritative_head),
        warnings=tuple(warnings),
    )
