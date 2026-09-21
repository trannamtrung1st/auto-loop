"""Configuration models and load/save helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auto_loop.exits import ExitCode

CONFIG_VERSION = 1
CONTEXT_VERSION = 1

USER_CONFIG_FILENAME = "auto-loop.yaml"
USER_OVERLAY_KEYS = frozenset({"models", "run"})

DEFAULT_PROTECTED_FILES: tuple[str, ...] = (
    "auto-loop.yaml",
    "goal.md",
    ".auto-loop/task.md",
    ".auto-loop/config.yaml",
    ".auto-loop/context.yaml",
    ".auto-loop/agents/planner.md",
    ".auto-loop/agents/worker.md",
    ".auto-loop/agents/reviewer.md",
    ".auto-loop/instructions/shared.md",
    ".auto-loop/instructions/planner.md",
    ".auto-loop/instructions/worker.md",
    ".auto-loop/instructions/reviewer.md",
)

ConsoleLevel = Literal["quiet", "normal", "verbose"]
InstructionMode = Literal["extend", "replace_role"]
ProviderType = Literal["cursor"]
AgentMode = Literal["agent", "ask"]
CursorCommand = Literal["agent", "cursor-agent"]


class ConfigurationError(Exception):
    """Invalid or unsupported configuration."""

    exit_code = ExitCode.CONFIG_ERROR


class CursorProviderSettings(BaseModel):
    command: CursorCommand = "agent"
    planner_extra_args: list[str] = Field(default_factory=lambda: ["--force"])
    worker_extra_args: list[str] = Field(default_factory=lambda: ["--force"])
    reviewer_extra_args: list[str] = Field(default_factory=list)


class ProviderSettings(BaseModel):
    type: ProviderType = "cursor"
    cursor: CursorProviderSettings = Field(default_factory=CursorProviderSettings)


class RoleAgentSettings(BaseModel):
    role_file: str
    model: str = "auto"
    mode: AgentMode


class InstructionRoleSettings(BaseModel):
    mode: InstructionMode = "extend"
    files: list[str] = Field(default_factory=list)


class InstructionSettings(BaseModel):
    shared: InstructionRoleSettings = Field(
        default_factory=lambda: InstructionRoleSettings(files=[".auto-loop/instructions/shared.md"])
    )
    planner: InstructionRoleSettings = Field(
        default_factory=lambda: InstructionRoleSettings(
            files=[".auto-loop/instructions/planner.md"]
        )
    )
    worker: InstructionRoleSettings = Field(
        default_factory=lambda: InstructionRoleSettings(
            files=[".auto-loop/instructions/worker.md"]
        )
    )
    reviewer: InstructionRoleSettings = Field(
        default_factory=lambda: InstructionRoleSettings(
            files=[".auto-loop/instructions/reviewer.md"]
        )
    )


class GitPolicySettings(BaseModel):
    require_repository: bool = True
    require_clean_product_start: bool = True
    require_clean_product_before_review: bool = True
    require_worker_commits: bool = True
    protect_approved_history: bool = True


class LimitSettings(BaseModel):
    max_turns: int = Field(default=100, ge=1)
    max_runtime_minutes: int = Field(default=480, ge=1)
    agent_timeout_seconds: int = Field(default=3600, ge=1)
    agent_idle_timeout_seconds: int = Field(default=300, ge=1)
    provider_retries: int = Field(default=2, ge=0)
    protocol_retries: int = Field(default=1, ge=0)
    max_consecutive_worker_no_progress: int = Field(default=3, ge=1)


class ProtectionSettings(BaseModel):
    product_exclude: list[str] = Field(default_factory=lambda: [".auto-loop/**", "auto-loop.yaml"])
    protected_files: list[str] = Field(default_factory=list)


class LoggingSettings(BaseModel):
    console: ConsoleLevel = "normal"
    retain_raw_streams: bool = True
    event_log: str = ".auto-loop/runtime/events.jsonl"
    max_run_history: int = Field(default=20, ge=1)


class AutoLoopConfig(BaseModel):
    version: int = CONFIG_VERSION
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    agents: dict[str, RoleAgentSettings] = Field(default_factory=dict)
    instructions: InstructionSettings = Field(default_factory=InstructionSettings)
    context_file: str = ".auto-loop/context.yaml"
    task_file: str = ".auto-loop/task.md"
    plan_file: str = ".auto-loop/plan.md"
    reviews_dir: str = ".auto-loop/reviews"
    git: GitPolicySettings = Field(default_factory=GitPolicySettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    protection: ProtectionSettings = Field(default_factory=ProtectionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {value}; expected version {CONFIG_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def default_agents(self) -> AutoLoopConfig:
        agents = dict(self.agents)
        if "planner" not in agents:
            agents["planner"] = RoleAgentSettings(
                role_file=".auto-loop/agents/planner.md",
                model="auto",
                mode="agent",
            )
        if "worker" not in agents:
            agents["worker"] = RoleAgentSettings(
                role_file=".auto-loop/agents/worker.md",
                model="auto",
                mode="agent",
            )
        if "reviewer" not in agents:
            agents["reviewer"] = RoleAgentSettings(
                role_file=".auto-loop/agents/reviewer.md",
                model="auto",
                mode="ask",
            )
        object.__setattr__(self, "agents", agents)
        merged_protected = list(self.protection.protected_files)
        for path in DEFAULT_PROTECTED_FILES:
            if path not in merged_protected:
                merged_protected.append(path)
        if merged_protected != self.protection.protected_files:
            object.__setattr__(
                self,
                "protection",
                self.protection.model_copy(update={"protected_files": merged_protected}),
            )
        return self

    @model_validator(mode="after")
    def validate_agents(self) -> AutoLoopConfig:
        for role in ("planner", "worker", "reviewer"):
            if role not in self.agents:
                raise ValueError(f"Missing required agent role: {role}")
            agent = self.agents[role]
            if agent.mode not in ("agent", "ask"):
                raise ValueError(f"Invalid mode for {role}: {agent.mode}")
        if self.agents["reviewer"].mode != "ask":
            raise ValueError("Reviewer agent must use ask mode")
        return self


def default_config() -> AutoLoopConfig:
    return AutoLoopConfig()


def config_path(repo: Path) -> Path:
    """Internal resolved configuration snapshot (tool-managed)."""
    return repo / ".auto-loop" / "config.yaml"


def user_config_path(repo: Path) -> Path:
    """User-owned configuration file."""
    return repo / USER_CONFIG_FILENAME


def resolved_config_snapshot_path(repo: Path) -> Path:
    return repo / ".auto-loop" / "runtime" / "config.resolved.yaml"


def load_config(path: Path) -> AutoLoopConfig:
    raw = _load_yaml_mapping(path)
    try:
        return AutoLoopConfig.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(_format_validation_error(path, exc)) from exc


def dump_config(config: AutoLoopConfig) -> str:
    data = config.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def load_config_from_repo(repo: Path) -> AutoLoopConfig:
    """Resolve configuration: defaults < internal snapshot < auto-loop.yaml."""
    user_path = user_config_path(repo)
    snapshot_path = config_path(repo)
    if not user_path.is_file() and not snapshot_path.is_file():
        raise ConfigurationError(
            "No Auto Loop configuration found.\n\n"
            f"Create one with:\n  auto-loop init\n\nExpected: {USER_CONFIG_FILENAME}"
        )

    if snapshot_path.is_file():
        config = load_config(snapshot_path)
    else:
        config = default_config()
    if user_path.is_file():
        config = overlay_user_config(config, user_path)
    return config


def load_resolved_config_from_repo(repo: Path) -> AutoLoopConfig:
    """Load the frozen resolved snapshot for an in-progress run (no user yaml re-overlay)."""
    for path in (config_path(repo), resolved_config_snapshot_path(repo)):
        if path.is_file():
            return load_config(path)
    return load_config_from_repo(repo)


def write_resolved_config(repo: Path, config: AutoLoopConfig) -> None:
    """Write the internal resolved snapshot used by the rest of the controller."""
    text = dump_config(config)
    snapshot = config_path(repo)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(text, encoding="utf-8")
    runtime_snapshot = resolved_config_snapshot_path(repo)
    runtime_snapshot.parent.mkdir(parents=True, exist_ok=True)
    runtime_snapshot.write_text(text, encoding="utf-8")


def parse_config_dict(data: dict[str, Any]) -> AutoLoopConfig:
    try:
        return AutoLoopConfig.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(str(exc)) from exc


def overlay_user_config(base: AutoLoopConfig, path: Path) -> AutoLoopConfig:
    raw = _load_yaml_mapping(path)
    models = raw.get("models")
    run = raw.get("run")
    rest = {key: value for key, value in raw.items() if key not in USER_OVERLAY_KEYS}
    merged = base.model_dump(mode="json")
    _deep_merge(merged, rest)
    try:
        config = AutoLoopConfig.model_validate(merged)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(_format_validation_error(path, exc)) from exc
    try:
        return apply_user_overlay(config, models=models, run=run)
    except ConfigurationError as exc:
        raise ConfigurationError(_format_validation_error(path, exc)) from exc


def apply_user_overlay(
    config: AutoLoopConfig,
    *,
    models: Any = None,
    run: Any = None,
) -> AutoLoopConfig:
    updates: dict[str, Any] = {}
    if models is not None:
        if not isinstance(models, dict):
            raise ConfigurationError("models must be a mapping of role names to model ids")
        agents = dict(config.agents)
        for role in ("planner", "worker", "reviewer"):
            if role not in models:
                continue
            model = models[role]
            if model is None:
                continue
            if not isinstance(model, str) or not model.strip():
                raise ConfigurationError(f"models.{role} must be a non-empty string")
            agents[role] = agents[role].model_copy(update={"model": model.strip()})
        unknown = [key for key in models if key not in {"planner", "worker", "reviewer"}]
        if unknown:
            raise ConfigurationError(
                "Unknown models role(s): " + ", ".join(str(key) for key in unknown)
            )
        updates["agents"] = agents
    if run is not None:
        if not isinstance(run, dict):
            raise ConfigurationError("run must be a mapping")
        allowed = {"max_turns", "max_runtime_minutes"}
        unknown_run = [key for key in run if key not in allowed]
        if unknown_run:
            raise ConfigurationError(
                "Unknown run field(s): " + ", ".join(str(key) for key in unknown_run)
            )
        limits_update: dict[str, Any] = {}
        if "max_turns" in run and run["max_turns"] is not None:
            limits_update["max_turns"] = run["max_turns"]
        if "max_runtime_minutes" in run and run["max_runtime_minutes"] is not None:
            limits_update["max_runtime_minutes"] = run["max_runtime_minutes"]
        if limits_update:
            try:
                updates["limits"] = config.limits.model_copy(update=limits_update)
            except (ValueError, ValidationError) as exc:
                raise ConfigurationError(str(exc)) from exc
    if not updates:
        return config
    return config.model_copy(update=updates)


def dump_user_config(*, models: dict[str, str], run: dict[str, int] | None = None) -> str:
    data: dict[str, Any] = {
        "models": {
            "planner": models.get("planner", "auto"),
            "worker": models.get("worker", "auto"),
            "reviewer": models.get("reviewer", "auto"),
        }
    }
    if run:
        data["run"] = run
    header = (
        "# User-owned Auto Loop configuration.\n"
        "# Tool-managed state lives under .auto-loop/ and is created on `auto-loop run`.\n\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid {path.name}\n\nMalformed YAML: {exc}\n\nFix the file and run again."
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"Invalid {path.name}\n\nConfiguration root must be a mapping.\n\n"
            "Fix the field above and run again."
        )
    return raw


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _format_validation_error(path: Path, exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        lines = [f"Invalid {path.name}", ""]
        for error in exc.errors():
            loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            message = error.get("msg", str(error))
            if loc:
                lines.append(f"{loc}:")
                lines.append(f"  {message}")
            else:
                lines.append(message)
            lines.append("")
        lines.append("Fix the field above and run again.")
        return "\n".join(lines).rstrip()
    return f"Invalid {path.name}\n\n{exc}\n\nFix the field above and run again."
