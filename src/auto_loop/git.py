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

    def __init__(
        self,
        message: str,
        *,
        commits: dict[str, str] | None = None,
        paths: tuple[str, ...] | list[str] | None = None,
        details: tuple[str, ...] | list[str] | None = None,
        state_preserved: bool = True,
    ) -> None:
        super().__init__(message)
        self.commits = dict(commits or {})
        self.paths = tuple(paths or ())
        self.details = tuple(details or ())
        self.state_preserved = state_preserved


class ReviewRequestError(GitProtocolError):
    """Worker review request the agent can correct on the same session.

    Raised for invalid explicit targets, empty evidence, and similar request
    payload mistakes. Fatal Git integrity failures remain plain ``GitProtocolError``.
    """


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


def _git_command_cwd(path: Path) -> Path:
    """Existing directory to use as Git subprocess cwd for paths that may not exist yet."""
    candidate = path.resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def git_worktree_root(path: Path) -> Path | None:
    """Return the enclosing Git worktree root for `path`, or None if not inside one."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=_git_command_cwd(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    return Path(text).resolve()


def is_path_git_ignored(path: Path) -> bool | None:
    """Whether `path` is ignored by Git.

    Returns None when `path` is not inside a Git worktree.

    Uses ``git check-ignore``. When the artifact directory does not exist yet,
    probes with a temporary file inside it (removed before returning) because
    Git only matches some directory rules against existing paths.
    """
    worktree = git_worktree_root(path)
    if worktree is None:
        return None
    try:
        rel = path.resolve().relative_to(worktree)
    except ValueError:
        return None
    rel_text = rel.as_posix()

    def _check_ignore(target: str) -> int:
        return subprocess.run(
            ["git", "check-ignore", "-q", "--", target],
            cwd=worktree,
            capture_output=True,
            check=False,
        ).returncode

    code = _check_ignore(rel_text)
    if code == 0:
        return True
    if code != 1:
        return False
    if path.exists():
        return False

    created_dir = False
    probe_name = ".auto-loop-doctor-ignore-probe"
    probe = path / probe_name
    try:
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created_dir = True
        probe.write_text("", encoding="utf-8")
        probe_rel = f"{rel_text.rstrip('/')}/{probe_name}"
        ignored = _check_ignore(probe_rel) == 0
    except OSError:
        ignored = False
    finally:
        if probe.is_file():
            probe.unlink(missing_ok=True)
        if created_dir and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return ignored


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return _run_git(repo, "rev-parse", ref)
    except GitError as exc:
        raise GitProtocolError(f"Unknown Git ref: {ref}") from exc


def resolve_requested_commit(repo: Path, ref: str, *, field: str) -> str:
    """Resolve a commit ref supplied by the worker in a review request."""
    try:
        return resolve_commit(repo, ref)
    except GitProtocolError as exc:
        raise ReviewRequestError(f"Worker {field} is not a valid Git ref: {ref}") from exc


def git_has_commits(repo: Path) -> bool:
    """True when HEAD resolves to a commit (repository is not unborn)."""
    if not is_git_repository(repo):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def head_commit_optional(repo: Path) -> str | None:
    """Return HEAD when the repository has commits; otherwise None (unborn or no Git)."""
    if not git_has_commits(repo):
        return None
    return resolve_commit(repo, "HEAD")


def head_commit(repo: Path) -> str:
    resolved = head_commit_optional(repo)
    if resolved is None:
        raise GitProtocolError("Git repository has no commits (unborn HEAD)")
    return resolved


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


def commit_exists(repo: Path, ref: str) -> bool:
    """True when ``ref`` names a commit object that is still in the database."""
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def list_commit_shas(repo: Path, ref: str = "HEAD") -> list[str]:
    """Commits reachable from ``ref``, newest first."""
    text = _run_git(repo, "rev-list", ref)
    if not text:
        return []
    return text.splitlines()


def assert_approved_baseline_ancestry(repo: Path, last_approved_commit: str) -> None:
    head = head_commit(repo)
    approved = resolve_commit(repo, last_approved_commit)
    if not is_ancestor(repo, approved, head):
        raise GitProtocolError(
            "Approved baseline is no longer an ancestor of HEAD (history rewrite detected)",
            commits={"Approved": approved, "HEAD": head},
        )


def format_git_protocol_error(
    exc: GitProtocolError,
    *,
    approved: str | None = None,
    head: str | None = None,
) -> str:
    """Terminal text for a Git protocol failure: invariant, SHAs/paths, and state."""
    invariant = str(exc).strip() or "Git protocol invariant failed"
    lines = ["Git protocol error", f"Invariant: {invariant}"]
    lines.extend(exc.details)
    commits = dict(exc.commits)
    if approved and "Approved" not in commits:
        commits["Approved"] = approved
    if head and "HEAD" not in commits:
        commits["HEAD"] = head
    ordered: list[tuple[str, str]] = []
    for label in ("Approved", "HEAD"):
        if label in commits:
            ordered.append((label, commits.pop(label)))
    ordered.extend(commits.items())
    for label, sha in ordered:
        lines.append(f"{label}: {sha}")
    if exc.paths:
        shown = list(exc.paths[:8])
        extra = len(exc.paths) - len(shown)
        path_text = ", ".join(shown)
        if extra > 0:
            path_text = f"{path_text} (+{extra} more)"
        lines.append(f"Paths: {path_text}")
    lines.append("State: preserved" if exc.state_preserved else "State: not preserved")
    return "\n".join(lines)


def normalize_batch_range(
    repo: Path,
    *,
    last_approved_commit: str,
    worker_base_commit: str | None = None,
    worker_head_commit: str | None = None,
    allow_empty: bool = False,
    excludes: tuple[str, ...] | None = None,
    require_clean: bool = True,
    protect_history: bool = True,
) -> NormalizedBatchRange:
    """Normalize a Git review range to authoritative last_approved..HEAD."""
    from auto_loop.product_state import DEFAULT_PRODUCT_EXCLUDES, assert_clean_product_tree

    if protect_history:
        assert_approved_baseline_ancestry(repo, last_approved_commit)
    if require_clean:
        assert_clean_product_tree(repo, excludes=excludes or DEFAULT_PRODUCT_EXCLUDES)

    authoritative_base = resolve_commit(repo, last_approved_commit)
    authoritative_head = head_commit(repo)

    warnings: list[str] = []
    if worker_base_commit:
        worker_base = resolve_requested_commit(
            repo, worker_base_commit, field="base_commit"
        )
        if worker_base != authoritative_base:
            warnings.append(
                "Worker base_commit "
                f"{worker_base[:7]} does not match last_approved_commit "
                f"{authoritative_base[:7]}; using approved baseline"
            )
    if worker_head_commit:
        worker_head = resolve_requested_commit(repo, worker_head_commit, field="head_commit")
        if worker_head != authoritative_head:
            raise ReviewRequestError(
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
