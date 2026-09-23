"""Normalize an explicit v2 run manifest into artifact-root runtime state."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from auto_loop.config import (
    AutoLoopConfig,
    load_resolved_config_optional,
    write_resolved_config,
)
from auto_loop.context_manifest import validate_context
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import materialize_artifact_layout, reset_run_scoped_workspace
from auto_loop.manifest import RunManifestSource, derive_protection
from auto_loop.paths import PathContainmentError, assert_contained, posix_rel, resolved_artifact_root
from auto_loop.task_resources import (
    TaskResourceError,
    assert_frozen_task_resources,
    materialize_task_resource_snapshots,
    resolve_task_resources,
    task_resources_root,
)


class RunInputError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


EXISTING_RUN_MESSAGE = """A run is already in progress.

Your existing run was not replaced.

Resume it:
  auto-loop resume {config}

Inspect it:
  auto-loop status {config}"""

NO_RESUME_MESSAGE = """No resumable Auto Loop run found.

Start one:
  auto-loop run {config}"""

MISSING_TASK_MESSAGE = """Task source is missing or empty: {path}

The run manifest must point at a readable task file with non-whitespace content."""


@dataclass(frozen=True)
class RunInputs:
    resume_only: bool = False


@dataclass
class PreparedRun:
    config: AutoLoopConfig
    goal_text: str
    is_resume: bool
    user_config_rel: str
    source: RunManifestSource
    workspace: Path
    artifact_root: Path


def _artifact_root_for(repo: Path, config: AutoLoopConfig) -> Path:
    return resolved_artifact_root(repo, config.artifacts.root)


def load_matching_blocked_record(repo: Path, artifact_root: Path | None = None):
    from auto_loop.runtime import load_lifecycle_state
    from auto_loop.terminal_records import load_blocked_record

    state = load_lifecycle_state(repo, artifact_root)
    record = load_blocked_record(repo, artifact_root)
    if record is None or state is None:
        return None
    if record.lifecycle_id != state.lifecycle_id:
        return None
    return record


def load_matching_completion_record(repo: Path, artifact_root: Path | None = None):
    from auto_loop.runtime import load_lifecycle_state
    from auto_loop.terminal_records import load_completion_record

    state = load_lifecycle_state(repo, artifact_root)
    record = load_completion_record(repo, artifact_root)
    if record is None or state is None:
        return None
    if record.lifecycle_id != state.lifecycle_id:
        return None
    return record


def has_lifecycle_state(repo: Path, artifact_root: Path | None = None) -> bool:
    from auto_loop.runtime import load_lifecycle_state

    return load_lifecycle_state(repo, artifact_root) is not None


def has_completion_record(repo: Path, artifact_root: Path | None = None) -> bool:
    return load_matching_completion_record(repo, artifact_root) is not None


def has_suspended_blocked_record(repo: Path, artifact_root: Path | None = None) -> bool:
    return load_matching_blocked_record(repo, artifact_root) is not None


def has_terminal_record(repo: Path, artifact_root: Path | None = None) -> bool:
    """True when the lifecycle reached idempotent completion (not blocked suspension)."""
    return has_completion_record(repo, artifact_root)


def has_active_lifecycle(repo: Path, artifact_root: Path | None = None) -> bool:
    """True when a lifecycle exists and has not completed idempotently."""
    if not has_lifecycle_state(repo, artifact_root):
        return False
    return not has_completion_record(repo, artifact_root)


def _archive_repo_file(
    repo: Path,
    archive_dir: Path,
    source: Path,
    *,
    move: bool = False,
) -> None:
    repo_root = repo.resolve()
    resolved_source = source.resolve()
    if not resolved_source.is_file():
        return
    try:
        rel = resolved_source.relative_to(repo_root)
    except ValueError as exc:
        raise RunInputError(f"Configured run path escapes workspace: {source}") from exc
    dest = (archive_dir / rel).resolve()
    try:
        dest.relative_to(archive_dir.resolve())
    except ValueError as exc:
        raise RunInputError(f"Configured run path escapes archive: {source}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(resolved_source), str(dest))
    else:
        shutil.copy2(resolved_source, dest)
        resolved_source.unlink()


def _iter_archive_files(root: Path) -> list[Path]:
    if root.is_symlink():
        return [root]
    if not root.exists():
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if child.is_symlink():
                files.append(child)
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            files.append(current / name)
    return files


def _assert_archive_paths_contained(repo: Path, config: AutoLoopConfig) -> None:
    from auto_loop.config import resolved_config_snapshot_path
    from auto_loop.runtime import state_path
    from auto_loop.terminal_records import blocked_path, completion_path

    artifact_root = _artifact_root_for(repo, config)
    candidates: list[Path] = [
        repo / config.plan_file,
        repo / config.task_file,
        repo / config.event_log,
        state_path(repo, artifact_root),
        completion_path(repo, artifact_root),
        blocked_path(repo, artifact_root),
        resolved_config_snapshot_path(artifact_root),
    ]
    reviews = repo / config.reviews_dir
    if reviews.is_dir():
        candidates.extend(path for path in reviews.iterdir() if path.is_file())
    candidates.extend(_iter_archive_files(task_resources_root(artifact_root)))

    repo_root = repo.resolve()
    for source in candidates:
        resolved = source.resolve()
        if not resolved.is_file():
            continue
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise RunInputError(f"Configured run path escapes workspace: {source}") from exc


def _assert_archive_dir_contained(repo: Path, archive_dir: Path) -> None:
    try:
        assert_contained(repo, archive_dir, label="Run archive path")
    except PathContainmentError as exc:
        raise RunInputError(str(exc)) from exc


def clear_prior_run_for_new_goal(
    repo: Path,
    *,
    config: AutoLoopConfig,
    artifact_root: Path | None = None,
) -> None:
    """Archive run-scoped artifacts and remove terminal/lifecycle state for a fresh goal."""
    from auto_loop.config import resolved_config_snapshot_path
    from auto_loop.runtime import state_path
    from auto_loop.terminal_records import blocked_path, completion_path

    root = artifact_root if artifact_root is not None else _artifact_root_for(repo, config)
    label = _prior_run_archive_label(repo, root)
    archive_dir = root / "runtime" / "archives" / label

    _assert_archive_paths_contained(repo, config)
    _assert_archive_dir_contained(repo, archive_dir)

    archive_dir.mkdir(parents=True, exist_ok=True)

    reviews = repo / config.reviews_dir
    if reviews.is_dir():
        for path in reviews.iterdir():
            if path.is_file():
                _archive_repo_file(repo, archive_dir, path, move=True)

    for rel in (config.plan_file, config.task_file):
        _archive_repo_file(repo, archive_dir, repo / rel)

    _archive_task_resources(repo, archive_dir, root)
    _archive_repo_file(repo, archive_dir, repo / config.event_log)
    _archive_repo_file(repo, archive_dir, state_path(repo, root))

    for path in (
        completion_path(repo, root),
        blocked_path(repo, root),
        resolved_config_snapshot_path(root),
    ):
        _archive_repo_file(repo, archive_dir, path)


def _archive_task_resources(repo: Path, archive_dir: Path, artifact_root: Path) -> None:
    """Move the previous lifecycle's frozen task resources into its archive."""
    root = task_resources_root(artifact_root)
    if not root.exists() and not root.is_symlink():
        return
    for path in _iter_archive_files(root):
        if path.is_symlink():
            path.unlink()
            continue
        _archive_repo_file(repo, archive_dir, path, move=True)
    if root.is_symlink():
        root.unlink()
        return
    if root.exists():
        shutil.rmtree(root)


def _prior_run_archive_label(repo: Path, artifact_root: Path) -> str:
    from auto_loop.runtime import load_lifecycle_state

    state = load_lifecycle_state(repo, artifact_root)
    if state is not None:
        return state.lifecycle_id
    record = load_matching_completion_record(repo, artifact_root)
    if record is not None:
        return record.lifecycle_id
    blocked = load_matching_blocked_record(repo, artifact_root)
    if blocked is not None:
        return blocked.lifecycle_id
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot_task(artifact_root: Path, text: str) -> Path:
    path = artifact_root / "task.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text if text.endswith("\n") else f"{text}\n"
    path.write_text(payload, encoding="utf-8")
    return path


def goal_summary(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading[:160]
            continue
        return stripped[:160]
    return "(empty)"


def _stored_goal_text(artifact_root: Path) -> str | None:
    path = artifact_root / "task.md"
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8")
    return None


def validate_task_source(source: RunManifestSource) -> str:
    path = source.task_source
    if not path.exists():
        raise RunInputError(MISSING_TASK_MESSAGE.format(path=path))
    if not path.is_file():
        raise RunInputError(f"Task source is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunInputError(f"Task source is not readable: {path}") from exc
    if not text.strip():
        raise RunInputError(MISSING_TASK_MESSAGE.format(path=path))
    return text


def _reraise_task_resource(exc: TaskResourceError) -> NoReturn:
    raise RunInputError(str(exc)) from exc


def validate_manifest_inputs(source: RunManifestSource) -> str:
    """Validate task, task resources, context, and instructions before mutating run state."""
    from auto_loop.instructions import validate_custom_instruction_files

    goal_text = validate_task_source(source)
    try:
        resolve_task_resources(source.workspace, source.config.task.resources)
    except TaskResourceError as exc:
        _reraise_task_resource(exc)
    context = validate_context(source.workspace, source.config.context)
    if not context.ok_for_run:
        messages = [issue.message for issue in context.issues if issue.severity == "error"]
        raise RunInputError("; ".join(messages) or "Invalid context resources")
    instruction_errors = validate_custom_instruction_files(source.workspace, source.config)
    if instruction_errors:
        raise RunInputError("; ".join(instruction_errors))
    return goal_text


def _config_label(source: RunManifestSource) -> str:
    rel = workspace_rel_or_absolute(source.workspace, source.path)
    return rel


def workspace_rel_or_absolute(workspace: Path, path: Path) -> str:
    from auto_loop.paths import workspace_relative

    rel = workspace_relative(workspace, path)
    return rel if rel is not None else str(path)


def prepare_repo_for_run(
    source: RunManifestSource,
    *,
    resume: bool = False,
) -> PreparedRun:
    """Materialize artifact state and freeze config for a new run, or load a frozen snapshot."""
    repo = source.workspace
    artifact_root = source.artifact_root
    config_label = _config_label(source)

    has_lifecycle = has_lifecycle_state(repo, artifact_root)
    if resume and has_lifecycle:
        frozen = load_resolved_config_optional(artifact_root)
        if frozen is None:
            raise RunInputError(
                "A previous run exists but its resolved snapshot is missing. "
                f"Inspect {posix_rel(str(artifact_root.relative_to(repo))) if artifact_root.is_relative_to(repo) else artifact_root} "
                "or start a new run after the current lifecycle ends."
            )
        stored = _stored_goal_text(artifact_root)
        if not stored:
            raise RunInputError(
                "A previous run exists but its stored task snapshot is missing. "
                "Inspect the artifact root or start a new repository checkout."
            )
        try:
            assert_frozen_task_resources(artifact_root, frozen.task.resources)
        except TaskResourceError as exc:
            _reraise_task_resource(exc)
        return PreparedRun(
            config=frozen,
            goal_text=stored,
            is_resume=True,
            user_config_rel=config_label,
            source=source,
            workspace=repo,
            artifact_root=artifact_root,
        )

    if not resume and has_active_lifecycle(repo, artifact_root):
        raise RunInputError(EXISTING_RUN_MESSAGE.format(config=config_label))

    if resume:
        raise RunInputError(NO_RESUME_MESSAGE.format(config=config_label))

    goal_text = validate_manifest_inputs(source)
    try:
        resources = resolve_task_resources(repo, source.config.task.resources)
        frozen_config = derive_protection(source)
    except TaskResourceError as exc:
        _reraise_task_resource(exc)

    if has_terminal_record(repo, artifact_root) or has_lifecycle:
        prior = load_resolved_config_optional(artifact_root) or frozen_config
        clear_prior_run_for_new_goal(repo, config=prior, artifact_root=artifact_root)
        reset_run_scoped_workspace(source)

    materialize_artifact_layout(source)
    snapshot_task(artifact_root, goal_text)
    try:
        materialize_task_resource_snapshots(repo, artifact_root, resources)
    except TaskResourceError as exc:
        _reraise_task_resource(exc)
    write_resolved_config(repo, frozen_config)

    return PreparedRun(
        config=frozen_config,
        goal_text=goal_text,
        is_resume=False,
        user_config_rel=config_label,
        source=source,
        workspace=repo,
        artifact_root=artifact_root,
    )
