"""Product working tree status excluding Auto Loop control state."""

from __future__ import annotations

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


def assert_clean_product_tree(
    repo: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> None:
    changes = list_product_changes(repo, excludes=excludes)
    if not changes:
        return
    paths = ", ".join(change.path for change in changes[:5])
    extra = "" if len(changes) <= 5 else f" (+{len(changes) - 5} more)"
    raise GitProtocolError(f"Product working tree is not clean: {paths}{extra}")


def iter_workspace_product_files(
    workspace: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> list[Path]:
    """Product files under ``workspace``, excluding control paths and escaping symlinks."""
    root = workspace.resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if is_control_path(rel, excludes):
            continue
        if path.is_symlink():
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
        files.append(path)
    return files


def filesystem_product_rows(
    workspace: Path,
    *,
    excludes: tuple[str, ...] = DEFAULT_PRODUCT_EXCLUDES,
) -> list[list[str]]:
    """Deterministic product snapshot when Git is unavailable or ``git.mode=off``."""
    from auto_loop.review_targets import fingerprint_path

    root = workspace.resolve()
    rows: list[list[str]] = []
    for path in iter_workspace_product_files(workspace, excludes=excludes):
        rel = path.relative_to(root).as_posix()
        digest, _ = fingerprint_path(path)
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
