"""Workspace and artifact path resolution."""

from __future__ import annotations

from pathlib import Path

DEFAULT_ARTIFACTS_ROOT = ".ai/auto-loop"


def posix_rel(value: str) -> str:
    return value.replace("\\", "/").rstrip("/")


def resolve_cli_path(path: Path) -> Path:
    """Resolve a CLI argument from the caller's current working directory."""
    return path.expanduser().resolve()


def resolve_repository_path(path: Path | None) -> Path:
    """Resolve a directory path (legacy helper; prefer explicit workspace from a manifest)."""
    if path is None:
        return Path.cwd().resolve()
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Repository path does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Repository path is not a directory: {resolved}")
    return resolved


def resolve_workspace(manifest_path: Path, workspace: str) -> Path:
    """Resolve `workspace` relative to the directory containing the run YAML."""
    raw = Path(workspace).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (manifest_path.parent / raw).resolve()


def resolve_workspace_path(workspace: Path, raw: str) -> Path:
    """Resolve a config path relative to workspace unless it is absolute."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace / path).resolve()


def artifact_root_path(workspace: Path, artifacts_root: str) -> Path:
    return resolve_workspace_path(workspace, artifacts_root)


def auto_loop_root(repo: Path, artifacts_root: str = DEFAULT_ARTIFACTS_ROOT) -> Path:
    """Resolved artifact root for a workspace (default v2 layout)."""
    return artifact_root_path(repo, artifacts_root)


def assert_contained(workspace: Path, path: Path, *, label: str) -> Path:
    """Require `path` to resolve inside `workspace` (follows symlinks)."""
    workspace_root = workspace.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise PathContainmentError(
            f"{label} must resolve inside the configured workspace: {path}"
        ) from exc
    return resolved


def workspace_relative(workspace: Path, path: Path) -> str | None:
    """Return a posix workspace-relative path, or None when `path` is outside."""
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    return posix_rel(str(rel))


def canonical_artifacts_root_setting(workspace: Path, artifact_root: Path) -> str:
    """Return a workspace-relative posix path suitable for config and Git excludes."""
    rel = workspace_relative(workspace, artifact_root)
    if rel is None:
        raise PathContainmentError(
            f"artifacts.root must resolve inside the configured workspace: {artifact_root}"
        )
    return rel


def configured_artifacts_root_rel(workspace: Path, root_raw: str) -> str | None:
    """Return the workspace-relative path Git uses for the configured setting (symlink-aware)."""
    raw = root_raw.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return workspace_relative(workspace, path)
    return posix_rel(raw)


def resolved_artifact_root(workspace: Path, artifacts_root: str) -> Path:
    """Resolve and normalize the configured artifact root directory."""
    return artifact_root_path(workspace, artifacts_root).resolve()


class PathContainmentError(ValueError):
    """A configured path escaped the workspace."""
