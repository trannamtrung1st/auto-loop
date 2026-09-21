"""Non-destructive migration from legacy Auto Loop layouts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from auto_loop.config import (
    USER_CONFIG_FILENAME,
    dump_user_config,
    load_config,
    user_config_path,
)
from auto_loop.exits import ExitCode
from auto_loop.paths import auto_loop_root


class MigrateError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


@dataclass
class LegacyLayout:
    user_config: bool = False
    internal_config: bool = False
    internal_task: bool = False
    internal_context: bool = False
    root_task: bool = False
    root_goal: bool = False
    root_context: bool = False

    @property
    def detected(self) -> bool:
        return any(
            (
                self.internal_config,
                self.internal_task,
                self.internal_context,
                self.root_task,
                self.root_goal,
                self.root_context,
            )
        )

    @property
    def needs_user_config(self) -> bool:
        return self.detected and not self.user_config

    @property
    def deprecated_inputs(self) -> list[str]:
        found: list[str] = []
        if self.root_task:
            found.append("task.md")
        if self.root_context:
            found.append("context.yaml")
        if self.root_goal:
            found.append("goal.md (keep using --goal-file)")
        return found


@dataclass
class MigrateResult:
    migrated: bool = False
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deprecated_inputs: list[str] = field(default_factory=list)
    message: str = ""


def detect_legacy_layout(repo: Path) -> LegacyLayout:
    root = auto_loop_root(repo)
    return LegacyLayout(
        user_config=user_config_path(repo).is_file(),
        internal_config=(root / "config.yaml").is_file(),
        internal_task=(root / "task.md").is_file(),
        internal_context=(root / "context.yaml").is_file(),
        root_task=(repo / "task.md").is_file(),
        root_goal=(repo / "goal.md").is_file(),
        root_context=(repo / "context.yaml").is_file(),
    )


def migrate_legacy_layout(repo: Path, *, force: bool = False) -> MigrateResult:
    layout = detect_legacy_layout(repo)
    result = MigrateResult(deprecated_inputs=layout.deprecated_inputs)
    if not layout.needs_user_config:
        if layout.user_config:
            result.skipped.append(USER_CONFIG_FILENAME)
            result.message = (
                "User-owned auto-loop.yaml already exists. Existing .auto-loop/ state was left unchanged."
            )
        else:
            result.message = "No legacy Auto Loop layout detected."
        return result

    dest = user_config_path(repo)
    if dest.exists() and not force:
        result.skipped.append(USER_CONFIG_FILENAME)
        result.message = "auto-loop.yaml already exists; not overwritten."
        return result

    models = {"planner": "auto", "worker": "auto", "reviewer": "auto"}
    run: dict[str, int] = {}
    internal = auto_loop_root(repo) / "config.yaml"
    if internal.is_file():
        try:
            cfg = load_config(internal)
        except Exception:
            cfg = None
        if cfg is not None:
            models = {
                "planner": cfg.agents["planner"].model,
                "worker": cfg.agents["worker"].model,
                "reviewer": cfg.agents["reviewer"].model,
            }
            run = {
                "max_turns": cfg.limits.max_turns,
                "max_runtime_minutes": cfg.limits.max_runtime_minutes,
            }
    dest.write_text(dump_user_config(models=models, run=run or None), encoding="utf-8")
    result.created.append(USER_CONFIG_FILENAME)
    result.migrated = True
    deprecated = "\n".join(f"  {item}" for item in result.deprecated_inputs) or "  (none)"
    result.message = "\n".join(
        [
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
        ]
    )
    return result
