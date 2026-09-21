"""Normalize user-facing run inputs into tool-managed .auto-loop state."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.config import (
    AutoLoopConfig,
    USER_CONFIG_FILENAME,
    load_config_from_repo,
    load_resolved_config_from_repo,
    write_resolved_config,
)
from auto_loop.context_manifest import validate_context_file
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import materialize_control_workspace, reset_run_scoped_workspace
from auto_loop.paths import auto_loop_root


class RunInputError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


MISSING_GOAL_MESSAGE = """No goal was provided.

Use one of:
  auto-loop run "Describe the goal"
  auto-loop run --goal-file goal.md"""

EXISTING_RUN_MESSAGE = """A run is already in progress.

Your existing run was not replaced.

Resume it:
  auto-loop resume

Inspect it:
  auto-loop status"""

NO_RESUME_MESSAGE = """No resumable Auto Loop run found.

Start one:
  auto-loop run "Describe the goal"
  auto-loop run --goal-file goal.md"""


@dataclass(frozen=True)
class RunInputs:
    goal_text: str | None = None
    goal_file: Path | None = None
    context_file: Path | None = None
    resume_only: bool = False
    minimal: bool = False


@dataclass
class PreparedRun:
    config: AutoLoopConfig
    goal_text: str
    is_resume: bool
    user_config_rel: str


def has_lifecycle_state(repo: Path) -> bool:
    from auto_loop.runtime import load_lifecycle_state

    return load_lifecycle_state(repo) is not None


def has_terminal_record(repo: Path) -> bool:
    from auto_loop.terminal_records import load_blocked_record, load_completion_record

    return load_completion_record(repo) is not None or load_blocked_record(repo) is not None


def has_active_lifecycle(repo: Path) -> bool:
    """True when a lifecycle exists and has not reached a terminal completion/blocked record."""
    if not has_lifecycle_state(repo):
        return False
    return not has_terminal_record(repo)


def _user_supplied_new_goal(inputs: RunInputs) -> bool:
    if inputs.goal_text and inputs.goal_text.strip():
        return True
    return inputs.goal_file is not None


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


def _assert_archive_paths_contained(repo: Path, config: AutoLoopConfig) -> None:
    from auto_loop.config import resolved_config_snapshot_path
    from auto_loop.runtime import state_path
    from auto_loop.terminal_records import blocked_path, completion_path

    candidates: list[Path] = [
        repo / config.plan_file,
        repo / config.context_file,
        repo / config.task_file,
        repo / config.logging.event_log,
        state_path(repo),
        completion_path(repo),
        blocked_path(repo),
        resolved_config_snapshot_path(repo),
    ]
    reviews = repo / config.reviews_dir
    if reviews.is_dir():
        candidates.extend(path for path in reviews.iterdir() if path.is_file())

    repo_root = repo.resolve()
    for source in candidates:
        resolved = source.resolve()
        if not resolved.is_file():
            continue
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise RunInputError(f"Configured run path escapes workspace: {source}") from exc


def clear_prior_run_for_new_goal(repo: Path, *, config: AutoLoopConfig) -> None:
    """Archive run-scoped artifacts and remove terminal/lifecycle state for a fresh goal."""
    from auto_loop.config import resolved_config_snapshot_path
    from auto_loop.runtime import state_path
    from auto_loop.terminal_records import blocked_path, completion_path

    label = _prior_run_archive_label(repo)
    root = auto_loop_root(repo)
    archive_dir = root / "runtime" / "archives" / label

    _assert_archive_paths_contained(repo, config)

    archive_dir.mkdir(parents=True, exist_ok=True)

    reviews = repo / config.reviews_dir
    if reviews.is_dir():
        for path in reviews.iterdir():
            if path.is_file():
                _archive_repo_file(repo, archive_dir, path, move=True)

    for rel in (config.plan_file, config.context_file, config.task_file):
        _archive_repo_file(repo, archive_dir, repo / rel)

    _archive_repo_file(repo, archive_dir, repo / config.logging.event_log)

    _archive_repo_file(repo, archive_dir, state_path(repo))

    for path in (
        completion_path(repo),
        blocked_path(repo),
        resolved_config_snapshot_path(repo),
    ):
        _archive_repo_file(repo, archive_dir, path)


def _prior_run_archive_label(repo: Path) -> str:
    from auto_loop.runtime import load_lifecycle_state
    from auto_loop.terminal_records import load_blocked_record, load_completion_record

    state = load_lifecycle_state(repo)
    if state is not None:
        return state.lifecycle_id
    record = load_completion_record(repo)
    if record is not None:
        return record.lifecycle_id
    blocked = load_blocked_record(repo)
    if blocked is not None:
        return blocked.lifecycle_id
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def resolve_optional_file(repo: Path, given: Path) -> Path:
    if given.is_absolute():
        return given
    cwd_candidate = (Path.cwd() / given).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (repo / given).resolve()


def _read_text_file(path: Path, *, missing_message: str) -> str:
    if not path.is_file():
        raise RunInputError(missing_message)
    return path.read_text(encoding="utf-8")


def snapshot_goal(repo: Path, text: str, *, config: AutoLoopConfig) -> Path:
    path = repo / config.task_file
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text if text.endswith("\n") else f"{text}\n"
    path.write_text(payload, encoding="utf-8")
    return path


def snapshot_context(repo: Path, source: Path, *, config: AutoLoopConfig) -> Path:
    dest = repo / config.context_file
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


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


def _legacy_goal_text(repo: Path) -> str | None:
    for rel in ("goal.md", "task.md"):
        path = repo / rel
        if path.is_file() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8")
    return None


def _stored_goal_text(repo: Path, config: AutoLoopConfig) -> str | None:
    path = repo / config.task_file
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8")
    return None


def _resolve_required_goal(repo: Path, inputs: RunInputs) -> str:
    if inputs.goal_text and inputs.goal_text.strip():
        return inputs.goal_text
    if inputs.goal_file is not None:
        goal_path = resolve_optional_file(repo, inputs.goal_file)
        goal_text = _read_text_file(
            goal_path,
            missing_message=f"Goal file not found: {inputs.goal_file}",
        )
        if not goal_text.strip():
            raise RunInputError(f"Goal file is empty: {inputs.goal_file}")
        return goal_text
    raise RunInputError(MISSING_GOAL_MESSAGE)


def _validate_new_run_inputs(
    repo: Path, inputs: RunInputs, _config: AutoLoopConfig
) -> tuple[str, Path | None]:
    """Validate goal and optional context before any destructive terminal-run reset."""
    goal_text = _resolve_required_goal(repo, inputs)
    context_path: Path | None = None
    if inputs.context_file is not None:
        context_path = resolve_optional_file(repo, inputs.context_file)
        if not context_path.is_file():
            raise RunInputError(f"Context file not found: {inputs.context_file}")
        result = validate_context_file(repo, context_path)
        if not result.ok_for_run:
            messages = [issue.message for issue in result.issues if issue.severity == "error"]
            raise RunInputError("; ".join(messages) or "Invalid context file")
    return goal_text, context_path


def prepare_repo_for_run(repo: Path, inputs: RunInputs | None = None) -> PreparedRun:
    """Materialize tool-managed state and freeze the canonical goal for this run."""
    inputs = inputs or RunInputs()
    if not user_config_or_legacy_exists(repo):
        raise RunInputError(
            "No Auto Loop configuration found.\n\n"
            f"Create one with:\n  auto-loop init\n\nExpected: {USER_CONFIG_FILENAME}"
        )

    has_lifecycle = has_lifecycle_state(repo)
    if inputs.resume_only and not has_lifecycle:
        raise RunInputError(NO_RESUME_MESSAGE)

    validated_goal: str | None = None
    validated_context: Path | None = None

    if _user_supplied_new_goal(inputs) and not inputs.resume_only:
        if has_active_lifecycle(repo):
            raise RunInputError(EXISTING_RUN_MESSAGE)
        new_config = load_config_from_repo(repo)
        validated_goal, validated_context = _validate_new_run_inputs(repo, inputs, new_config)
        if has_terminal_record(repo) or has_lifecycle:
            prior_config = load_resolved_config_from_repo(repo)
            clear_prior_run_for_new_goal(repo, config=prior_config)
            reset_run_scoped_workspace(repo, config=new_config, minimal=inputs.minimal)
            has_lifecycle = False

    materialize_control_workspace(repo, minimal=inputs.minimal, force=False)

    if has_lifecycle:
        config = load_resolved_config_from_repo(repo)
        stored = _stored_goal_text(repo, config)
        if not stored:
            raise RunInputError(
                "A previous run exists but its stored goal snapshot is missing. "
                "Inspect .auto-loop/ or start a new repository checkout."
            )
        return PreparedRun(
            config=config,
            goal_text=stored,
            is_resume=True,
            user_config_rel=(
                USER_CONFIG_FILENAME if (repo / USER_CONFIG_FILENAME).is_file() else ".auto-loop/config.yaml"
            ),
        )

    config = load_config_from_repo(repo)
    write_resolved_config(repo, config)

    if validated_goal is not None:
        goal_text = validated_goal
    else:
        goal_text = _legacy_goal_text(repo) or _stored_goal_text(repo, config)

    if not goal_text or not goal_text.strip():
        raise RunInputError(MISSING_GOAL_MESSAGE)

    snapshot_goal(repo, goal_text, config=config)

    if validated_context is not None:
        snapshot_context(repo, validated_context, config=config)
    elif inputs.context_file is not None:
        _, validated_context = _validate_new_run_inputs(repo, inputs, config)
        snapshot_context(repo, validated_context, config=config)

    user_rel = (
        USER_CONFIG_FILENAME if (repo / USER_CONFIG_FILENAME).is_file() else ".auto-loop/config.yaml"
    )
    return PreparedRun(
        config=load_config_from_repo(repo),
        goal_text=goal_text,
        is_resume=False,
        user_config_rel=user_rel,
    )


def user_config_or_legacy_exists(repo: Path) -> bool:
    return (repo / USER_CONFIG_FILENAME).is_file() or (
        auto_loop_root(repo) / "config.yaml"
    ).is_file()
