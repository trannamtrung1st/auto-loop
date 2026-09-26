"""Product working tree status excluding Auto Loop control state."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from auto_loop.git import GitProtocolError, head_commit_optional, is_git_repository
from auto_loop.paths import DEFAULT_ARTIFACTS_ROOT, posix_rel

if TYPE_CHECKING:
    from auto_loop.config import AutoLoopConfig

DEFAULT_PRODUCT_EXCLUDES = (f"{DEFAULT_ARTIFACTS_ROOT}/", f"{DEFAULT_ARTIFACTS_ROOT}/**")


@dataclass(frozen=True)
class ProductChange:
    path: str
    status: str


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def product_excludes(config: AutoLoopConfig) -> tuple[str, ...]:
    root = posix_rel(config.artifacts_root)
    items = [f"{root}/", f"{root}/**"]
    for extra in (*config.protection.product_exclude, *config.protection.protected_files):
        normalized = extra.replace("\\", "/").rstrip("/")
        if normalized and normalized not in items:
            items.append(normalized)
    return tuple(items)


def is_control_path(path: str, excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES) -> bool:
    normalized = _normalize_path(path)
    for pattern in excludes:
        prefix = pattern.rstrip("/").rstrip("*")
        if pattern.endswith("/**") or pattern.endswith("/*"):
            prefix = pattern[:-3].rstrip("/")
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def list_product_changes(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> list[ProductChange]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-uall"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitProtocolError((result.stderr or result.stdout or "git status failed").strip())

    changes: list[ProductChange] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if is_control_path(path, excludes):
            continue
        changes.append(ProductChange(path=path, status=xy.strip() or "??"))
    return changes


def is_product_tree_clean(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> bool:
    return not list_product_changes(repo, excludes=excludes)


def format_dirty_product_tree_message(
    changes: list[ProductChange],
) -> str:
    paths = ", ".join(change.path for change in changes[:5])
    extra = "" if len(changes) <= 5 else f" (+{len(changes) - 5} more)"
    return f"Product working tree is not clean: {paths}{extra}"


def dirty_product_tree_handoff_reason(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> str:
    changes = list_product_changes(repo, excludes=excludes)
    detail = format_dirty_product_tree_message(changes)
    return "\n".join(
        [
            "Review handoff rejected: product tree is dirty.",
            detail,
            "Reconcile the working tree.",
            (
                "Commit intended product changes or revert unintended changes, "
                "then emit a fresh review request."
            ),
        ]
    )


def assert_clean_product_tree(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> None:
    changes = list_product_changes(repo, excludes=excludes)
    if not changes:
        return
    raise GitProtocolError(
        format_dirty_product_tree_message(changes),
        paths=tuple(change.path for change in changes),
    )


def product_path_fingerprint(path: Path) -> str:
    """Fingerprint one workspace product path without following symlinks out of the tree."""
    from auto_loop.review_targets import fingerprint_path, sha256_bytes

    if path.is_symlink():
        target = os.readlink(path)
        return sha256_bytes(f"symlink:{target}".encode("utf-8"))
    digest, _ = fingerprint_path(path)
    return digest


def iter_workspace_product_paths(
    workspace: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> list[Path]:
    """Product files and symlinks under ``workspace``, excluding control paths.

    Symlinks are recorded by link text (``readlink``) and never followed into paths
    outside the workspace. Regular files are included by ``lstat`` type.
    """
    root = workspace.resolve()
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            path = current / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_control_path(rel, excludes):
                if name in dirnames:
                    dirnames.remove(name)
                continue
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISLNK(mode):
                paths.append(path)
                if name in dirnames:
                    dirnames.remove(name)
                continue
            if stat.S_ISREG(mode):
                paths.append(path)
    return sorted(paths)


def filesystem_product_rows(
    workspace: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> list[list[str]]:
    """Deterministic product snapshot when Git is unavailable or ``git.mode=off``."""
    root = workspace.resolve()
    rows: list[list[str]] = []
    for path in iter_workspace_product_paths(workspace, excludes=excludes):
        rel = path.relative_to(root).as_posix()
        digest = product_path_fingerprint(path)
        rows.append([rel, "fs", digest])
    rows.sort()
    return rows


def capture_product_working_fingerprint(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
    config: AutoLoopConfig | None = None,
) -> tuple[str | None, list[list[str]]]:
    """Snapshot product state for planner mutation checks.

    Each row is ``[path, status, content-fingerprint]``. With Git, status comes
    from ``git status``; without Git, status is ``fs`` for every product file.
    """
    from auto_loop.git_policy import git_usable

    use_git = False
    if config is not None:
        use_git = git_usable(repo, config)
    elif is_git_repository(repo):
        use_git = head_commit_optional(repo) is not None

    if not use_git:
        return None, filesystem_product_rows(repo, excludes=excludes)

    from auto_loop.review_targets import fingerprint_path, sha256_bytes

    head = head_commit_optional(repo)
    rows: list[list[str]] = []
    for change in list_product_changes(repo, excludes=excludes):
        resolved = repo / change.path
        if resolved.exists():
            digest, _ = fingerprint_path(resolved)
        else:
            digest = sha256_bytes(b"")
        rows.append([change.path, change.status, digest])
    rows.sort()
    return head, rows


def product_working_fingerprints_equal(
    before: list[list[str]] | None,
    after: list[list[str]] | None,
) -> bool:
    if before is None and after is None:
        return True
    if before is None or after is None:
        return False
    if all(len(row) >= 3 for row in before) and all(len(row) >= 3 for row in after):
        return before == after
    legacy_before = sorted([row[:2] for row in before])
    legacy_after = sorted([row[:2] for row in after])
    return legacy_before == legacy_after


def commit_product_tree_fingerprint(
    repo: Path,
    commit: str,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> str:
    """Content fingerprint of product paths stored in ``commit``.

    Control paths are omitted. Two commits match when they record the same
    product blobs, even if commit metadata or excluded paths differ.
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "-z", commit],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise GitProtocolError(stderr or f"git ls-tree {commit} failed")
    lines: list[str] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        meta, sep, path_b = record.partition(b"\t")
        if not sep:
            continue
        path = path_b.decode("utf-8", errors="surrogateescape")
        if is_control_path(path, excludes):
            continue
        lines.append(meta.decode("ascii", errors="replace") + "\t" + path)
    lines.sort()
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
