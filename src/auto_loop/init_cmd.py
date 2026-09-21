"""Initialize user-owned Auto Loop files and materialize tool-managed state."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from auto_loop.config import (
    USER_CONFIG_FILENAME,
    ConfigurationError,
    InstructionRoleSettings,
    InstructionSettings,
    load_config_from_repo,
    user_config_path,
    write_resolved_config,
)
from auto_loop.exits import ExitCode
from auto_loop.paths import auto_loop_root

RUNTIME_GITIGNORE_ENTRY = ".auto-loop/runtime/"


class InitError(Exception):
    """Initialization refused or failed."""

    exit_code = ExitCode.CONFIG_ERROR


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    migrated: bool = False
    deprecated_inputs: list[str] = field(default_factory=list)
    message: str = ""


def _read_template(relative: str) -> str:
    package = resources.files("auto_loop").joinpath("templates")
    return package.joinpath(relative).read_text(encoding="utf-8")


def _write_text(
    path: Path,
    content: str,
    *,
    force: bool,
    result: InitResult,
    rel: str,
) -> None:
    if path.exists() and not force:
        result.skipped.append(rel)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(rel)


def _ensure_dir(path: Path, result: InitResult, rel: str) -> None:
    if path.exists():
        result.skipped.append(rel)
        return
    path.mkdir(parents=True, exist_ok=True)
    result.created.append(rel)


def _user_config_yaml(minimal: bool) -> str:
    if not minimal:
        return _read_template("auto-loop.yaml")
    data = {
        "models": {"planner": "auto", "worker": "auto", "reviewer": "auto"},
        "instructions": {
            "shared": {"mode": "extend", "files": []},
            "planner": {"mode": "extend", "files": []},
            "worker": {"mode": "extend", "files": []},
            "reviewer": {"mode": "extend", "files": []},
        },
    }
    header = (
        "# User-owned Auto Loop configuration.\n"
        "# Tool-managed state lives under .auto-loop/ and is created on `auto-loop run`.\n\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def ensure_runtime_gitignore(repo: Path, result: InitResult | None = None) -> bool:
    """Append runtime ignore rules to .gitignore. Returns True when a write happened."""
    path = repo / ".gitignore"
    rel = ".gitignore"
    marker = RUNTIME_GITIGNORE_ENTRY
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        existing = {line.strip() for line in text.splitlines()}
        if marker in existing or ".auto-loop/runtime" in existing:
            if result is not None:
                result.skipped.append(rel)
            return False
        prefix = "" if text.endswith("\n") or text == "" else "\n"
        path.write_text(
            f"{text}{prefix}\n# Auto Loop ephemeral runtime state\n{marker}\n",
            encoding="utf-8",
        )
    else:
        path.write_text(
            f"# Auto Loop ephemeral runtime state\n{marker}\n",
            encoding="utf-8",
        )
    if result is not None:
        result.created.append(rel)
    return True


def materialize_control_workspace(
    repo: Path,
    *,
    force: bool = False,
    minimal: bool = False,
) -> InitResult:
    """Create tool-managed .auto-loop templates. Does not write user-owned files."""
    if not minimal:
        try:
            config = load_config_from_repo(repo)
            if not config.instructions.worker.files:
                minimal = True
        except ConfigurationError:
            pass
    root = auto_loop_root(repo)
    result = InitResult()
    planner_tpl = "agents/planner.minimal.md" if minimal else "agents/planner.md"
    worker_tpl = "agents/worker.minimal.md" if minimal else "agents/worker.md"
    reviewer_tpl = "agents/reviewer.minimal.md" if minimal else "agents/reviewer.md"
    _write_text(
        root / "context.yaml",
        _read_template("context.default.yaml"),
        force=force,
        result=result,
        rel=".auto-loop/context.yaml",
    )
    _write_text(
        root / "plan.md",
        _read_template("plan.md"),
        force=force,
        result=result,
        rel=".auto-loop/plan.md",
    )
    _write_text(
        root / "agents" / "planner.md",
        _read_template(planner_tpl),
        force=force,
        result=result,
        rel=".auto-loop/agents/planner.md",
    )
    _write_text(
        root / "agents" / "worker.md",
        _read_template(worker_tpl),
        force=force,
        result=result,
        rel=".auto-loop/agents/worker.md",
    )
    _write_text(
        root / "agents" / "reviewer.md",
        _read_template(reviewer_tpl),
        force=force,
        result=result,
        rel=".auto-loop/agents/reviewer.md",
    )
    if not minimal:
        for name in ("shared.md", "planner.md", "worker.md", "reviewer.md"):
            _write_text(
                root / "instructions" / name,
                _read_template(f"instructions/{name}"),
                force=force,
                result=result,
                rel=f".auto-loop/instructions/{name}",
            )
        _ensure_dir(root / "resources", result, ".auto-loop/resources/")
    _ensure_dir(root / "reviews", result, ".auto-loop/reviews/")
    _ensure_dir(root / "runtime", result, ".auto-loop/runtime/")
    return result


def bootstrap_workspace(
    repo: Path,
    *,
    force: bool = False,
    minimal: bool = False,
    goal: str = "Test task",
) -> InitResult:
    """Init + materialize + snapshot a goal. Intended for tests and fixtures."""
    result = run_init(repo, force=force, minimal=minimal)
    materialized = materialize_control_workspace(repo, force=force, minimal=minimal)
    result.created.extend(materialized.created)
    result.skipped.extend(materialized.skipped)
    config = load_config_from_repo(repo)
    if minimal:
        config.instructions = InstructionSettings(
            shared=InstructionRoleSettings(files=[]),
            planner=InstructionRoleSettings(files=[]),
            worker=InstructionRoleSettings(files=[]),
            reviewer=InstructionRoleSettings(files=[]),
        )
    write_resolved_config(repo, config)
    task_path = repo / config.task_file
    if force or not task_path.is_file() or task_path.read_text(encoding="utf-8").strip() == "":
        task_path.parent.mkdir(parents=True, exist_ok=True)
        text = goal if goal.endswith("\n") else f"{goal}\n"
        task_path.write_text(text, encoding="utf-8")
        if ".auto-loop/task.md" not in result.created:
            result.created.append(".auto-loop/task.md")
    return result


def _render_init_message(result: InitResult) -> str:
    lines = [
        "Auto Loop initialized.",
        "",
        f"Config: {USER_CONFIG_FILENAME}",
        "",
        "Next:",
        '  auto-loop run "Describe what you want to build"',
        "",
        "Or:",
        "  auto-loop run --goal-file goal.md",
        "",
        ".auto-loop/ is tool-managed state and will be created when a run starts.",
        "You normally do not need to edit that directory.",
        "",
        "Consider ignoring ephemeral runtime state in Git:",
        "  .auto-loop/runtime/",
    ]
    if result.migrated:
        deprecated = "\n".join(f"  {item}" for item in result.deprecated_inputs) or "  (none)"
        lines = [
            "Legacy Auto Loop layout detected.",
            "",
            "Created:",
            f"  {USER_CONFIG_FILENAME}",
            "",
            "Existing run state under .auto-loop/ was preserved.",
            "",
            "Deprecated user inputs:",
            deprecated,
            "",
            "Future runs should use:",
            "  auto-loop run --goal-file goal.md",
            "",
            *lines[4:],
        ]
    return "\n".join(lines)


def run_init(repo: Path, *, force: bool = False, minimal: bool = False) -> InitResult:
    """Create user-owned auto-loop.yaml and ignore rules. Do not create a goal."""
    from auto_loop.migrate_cmd import migrate_legacy_layout

    result = InitResult()
    migrated = migrate_legacy_layout(repo, force=force)
    if migrated.migrated:
        result.migrated = True
        result.deprecated_inputs = list(migrated.deprecated_inputs)
        result.created.extend(migrated.created)
        result.skipped.extend(migrated.skipped)
    else:
        config_file = user_config_path(repo)
        if config_file.exists() and not force:
            result.skipped.append(USER_CONFIG_FILENAME)
        else:
            _write_text(
                config_file,
                _user_config_yaml(minimal),
                force=True,
                result=result,
                rel=USER_CONFIG_FILENAME,
            )

    if force and auto_loop_root(repo).is_dir():
        materialized = materialize_control_workspace(repo, force=True, minimal=minimal)
        result.created.extend(materialized.created)
        result.skipped.extend(materialized.skipped)
        write_resolved_config(repo, load_config_from_repo(repo))

    result.message = _render_init_message(result)
    return result
