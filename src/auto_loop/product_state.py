"""Product working tree status excluding .auto-loop control state."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from auto_loop.git import GitProtocolError

DEFAULT_PRODUCT_EXCLUDES = (".auto-loop/", "auto-loop.yaml")


@dataclass(frozen=True)
class ProductChange:
    path: str
    status: str


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


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
