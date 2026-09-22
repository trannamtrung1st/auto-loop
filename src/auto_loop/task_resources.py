"""Resolve, snapshot, and describe authoritative task resources.

Task resources are explicit ``task.resources`` entries. Markdown links inside
the task entry are informational only and are never discovered from here.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import RoleName, render_resource_manifest
from auto_loop.exits import ExitCode
from auto_loop.paths import PathContainmentError, assert_contained, posix_rel

TASK_RESOURCES_DIRNAME = "task-resources"


class TaskResourceError(Exception):
    """A task resource failed validation or its frozen snapshot is unusable."""

    exit_code = ExitCode.CONFIG_ERROR


@dataclass(frozen=True)
class TaskResource:
    """One validated workspace file declared in ``task.resources``."""

    configured: str
    relative: str
    path: Path


def task_resources_root(artifact_root: Path) -> Path:
    return artifact_root / TASK_RESOURCES_DIRNAME


def snapshot_relative(resource_relative: str) -> str:
    return f"{TASK_RESOURCES_DIRNAME}/{resource_relative}"


def render_authoritative_task_resource_manifest(config: AutoLoopConfig) -> str:
    """Compact path manifest. Does not inline resource contents."""
    resources = list(config.task.resources)
    if not resources:
        return ""
    root = posix_rel(config.artifacts_root)
    lines = ["AUTHORITATIVE TASK RESOURCES", ""]
    for relative in resources:
        lines.append(f"- {relative}")
        lines.append(f"  Frozen snapshot: {root}/{snapshot_relative(relative)}")
        lines.append("")
    lines.append("Use the frozen snapshots for lifecycle decisions.")
    lines.append("task.md remains the highest-level authority.")
    return "\n".join(lines).rstrip()


def compose_turn_resource_manifest(config: AutoLoopConfig, role: RoleName) -> str:
    """Authoritative task resources, then lower-authority context."""
    authoritative = render_authoritative_task_resource_manifest(config)
    context = render_resource_manifest(config.context, role)
    if not authoritative:
        return context
    return f"{authoritative}\n\n{context}"


def resolve_task_resources(workspace: Path, resources: list[str]) -> list[TaskResource]:
    """Validate every configured task resource. Does not write lifecycle state."""
    resolved: list[TaskResource] = []
    seen: dict[str, str] = {}
    errors: list[str] = []
    for raw in resources:
        try:
            item = _resolve_one(workspace, raw)
        except TaskResourceError as exc:
            errors.append(str(exc))
            continue
        previous = seen.get(item.relative)
        if previous is not None:
            errors.append(
                f"Duplicate task resource: {raw} resolves to the same workspace path as {previous}"
            )
            continue
        seen[item.relative] = raw
        resolved.append(item)
    if errors:
        raise TaskResourceError("\n".join(errors))
    return resolved


def materialize_task_resource_snapshots(
    workspace: Path,
    artifact_root: Path,
    resources: list[TaskResource],
) -> None:
    """Copy validated resources into the tool-managed snapshot tree."""
    if not resources:
        return
    root = artifact_root.resolve()
    try:
        assert_contained(workspace, root, label="Task resource snapshot")
    except PathContainmentError as exc:
        raise TaskResourceError(str(exc)) from exc
    dest_root = task_resources_root(root)
    _ensure_directory(root, dest_root)
    for resource in resources:
        dest = dest_root.joinpath(*resource.relative.split("/"))
        _assert_lexical_child(root, dest)
        _ensure_directory(root, dest.parent)
        if dest.is_symlink():
            dest.unlink()
        try:
            data = resource.path.read_bytes()
        except OSError as exc:
            raise TaskResourceError(
                f"Task resource is missing or unreadable: {resource.configured}"
            ) from exc
        _atomic_write_bytes(dest, data)


def assert_frozen_task_resources(artifact_root: Path, resources: list[str]) -> None:
    """Require frozen copies. Never substitute the live workspace file."""
    root = artifact_root.resolve()
    errors: list[str] = []
    for relative in resources:
        if _relative_escapes(relative):
            errors.append(f"Frozen task resource escapes workspace: {relative}")
            continue
        path = task_resources_root(root).joinpath(*relative.split("/"))
        try:
            _assert_lexical_child(root, path)
        except TaskResourceError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(_missing_frozen_message(path))
            continue
        try:
            path.read_bytes()
        except OSError as exc:
            errors.append(f"{_missing_frozen_message(path)} ({exc})")
    if errors:
        raise TaskResourceError("\n".join(errors))


def frozen_task_resource_issues(artifact_root: Path, resources: list[str]) -> list[str]:
    try:
        assert_frozen_task_resources(artifact_root, resources)
    except TaskResourceError as exc:
        return [line for line in str(exc).splitlines() if line.strip()]
    return []


def _missing_frozen_message(path: Path) -> str:
    return (
        f"Frozen task resource is missing: {path}\n"
        "The lifecycle snapshot is incomplete, so resume cannot continue. "
        "Auto Loop will not fall back to the live workspace file."
    )


def _resolve_one(workspace: Path, raw: str) -> TaskResource:
    if not isinstance(raw, str):
        raise TaskResourceError(f"Task resource path must be a string: {raw!r}")
    configured = raw.strip()
    if not configured:
        raise TaskResourceError("Task resource path is empty")
    relative, resolved = _normalized_workspace_file(workspace, configured)
    if not resolved.exists():
        raise TaskResourceError(f"Task resource is missing or unreadable: {configured}")
    if resolved.is_dir():
        raise TaskResourceError(f"Task resource must be a file: {configured}")
    if not resolved.is_file():
        raise TaskResourceError(f"Task resource is not a regular file: {configured}")
    try:
        resolved.read_bytes()
    except OSError as exc:
        raise TaskResourceError(
            f"Task resource is missing or unreadable: {configured}"
        ) from exc
    return TaskResource(configured=configured, relative=relative, path=resolved)


def _normalized_workspace_file(workspace: Path, raw: str) -> tuple[str, Path]:
    workspace_root = workspace.resolve()
    text = raw.replace("\\", "/")
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        try:
            resolved = assert_contained(workspace_root, candidate, label="Task resource")
        except PathContainmentError as exc:
            raise TaskResourceError(f"Task resource escapes workspace: {raw}") from exc
        relative = _workspace_relative(workspace_root, resolved)
        if relative is None or _relative_escapes(relative):
            raise TaskResourceError(f"Task resource escapes workspace: {raw}")
        return relative, resolved

    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise TaskResourceError(f"Task resource escapes workspace: {raw}")
            parts.pop()
            continue
        if "\x00" in part:
            raise TaskResourceError(f"Task resource escapes workspace: {raw}")
        parts.append(part)
    if not parts:
        raise TaskResourceError(f"Task resource escapes workspace: {raw}")
    relative = "/".join(parts)
    lexical = workspace_root.joinpath(*parts)
    try:
        resolved = assert_contained(workspace_root, lexical, label="Task resource")
    except PathContainmentError as exc:
        raise TaskResourceError(f"Task resource escapes workspace: {raw}") from exc
    return relative, resolved


def _workspace_relative(workspace: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    return posix_rel(str(rel))


def _relative_escapes(relative: str) -> bool:
    if not relative or relative.startswith("/") or "\\" in relative:
        return True
    return any(part == ".." for part in relative.split("/"))


def _assert_lexical_child(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TaskResourceError(f"Task resource snapshot escapes workspace: {path}") from exc


def _ensure_directory(artifact_root: Path, directory: Path) -> None:
    _assert_lexical_child(artifact_root, directory)
    cursor = artifact_root
    if directory == artifact_root:
        return
    for part in directory.relative_to(artifact_root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            try:
                assert_contained(artifact_root, cursor, label="Task resource snapshot")
            except PathContainmentError as exc:
                raise TaskResourceError(
                    f"Task resource snapshot escapes workspace: {cursor}"
                ) from exc
            cursor = cursor.resolve()
            continue
        if cursor.exists() and not cursor.is_dir():
            raise TaskResourceError(f"Task resource snapshot path is not a directory: {cursor}")
        if not cursor.exists():
            cursor.mkdir()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.is_file() or tmp_path.is_symlink():
            try:
                tmp_path.unlink()
            except OSError:
                pass
