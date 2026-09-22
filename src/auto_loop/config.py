"""Version 2 run-manifest models and load/save helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auto_loop.context_manifest import ContextDocument
from auto_loop.exits import ExitCode
from auto_loop.paths import DEFAULT_ARTIFACTS_ROOT, artifact_root_path, posix_rel

CONFIG_VERSION = 2

ConsoleLevel = Literal["quiet", "normal", "verbose"]
InstructionMode = Literal["extend", "replace_role"]
ProviderType = Literal["cursor"]
AgentMode = Literal["agent", "ask"]
CursorCommand = Literal["agent", "cursor-agent"]

V1_UNSUPPORTED_MESSAGE = (
    "Unsupported Auto Loop config version 1.\n"
    "This release requires the single-manifest version 2 format."
)


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
    """Runtime agent slot settings. Model selection lives under top-level `models`."""

    role_file: str = ""
    model: str = "auto"
    mode: AgentMode = "agent"


class InstructionRoleSettings(BaseModel):
    mode: InstructionMode = "extend"
    files: list[str] = Field(default_factory=list)


class InstructionSettings(BaseModel):
    shared: InstructionRoleSettings = Field(default_factory=InstructionRoleSettings)
    planner: InstructionRoleSettings = Field(default_factory=InstructionRoleSettings)
    worker: InstructionRoleSettings = Field(default_factory=InstructionRoleSettings)
    reviewer: InstructionRoleSettings = Field(default_factory=InstructionRoleSettings)


class GitPolicySettings(BaseModel):
    require_repository: bool = True
    require_clean_product_start: bool = True
    require_clean_product_before_review: bool = True
    require_worker_commits: bool = True
    protect_approved_history: bool = True


class LimitSettings(BaseModel):
    """Normalized run limits (derived from public `run` settings)."""

    max_turns: int = Field(default=100, ge=1)
    max_runtime_minutes: int = Field(default=480, ge=1)
    agent_timeout_seconds: int = Field(default=3600, ge=1)
    agent_idle_timeout_seconds: int = Field(default=300, ge=1)
    provider_retries: int = Field(default=2, ge=0)
    protocol_retries: int = Field(default=1, ge=0)
    max_consecutive_worker_no_progress: int = Field(default=3, ge=1)


class ProtectionSettings(BaseModel):
    product_exclude: list[str] = Field(default_factory=list)
    protected_files: list[str] = Field(default_factory=list)


class LoggingSettings(BaseModel):
    console: ConsoleLevel = "normal"
    retain_raw_streams: bool = True
    max_run_history: int = Field(default=20, ge=1)


class ModelSettings(BaseModel):
    planner: str = "auto"
    worker: str = "auto"
    reviewer: str = "auto"


class RunSettings(BaseModel):
    max_turns: int = Field(default=100, ge=1)
    max_runtime_minutes: int = Field(default=480, ge=1)
    agent_timeout_seconds: int = Field(default=3600, ge=1)
    agent_idle_timeout_seconds: int = Field(default=300, ge=1)
    provider_retries: int = Field(default=2, ge=0)
    protocol_retries: int = Field(default=1, ge=0)
    max_consecutive_worker_no_progress: int = Field(default=3, ge=1)


class TaskSettings(BaseModel):
    source: str


class ArtifactSettings(BaseModel):
    root: str = DEFAULT_ARTIFACTS_ROOT


class AutoLoopConfig(BaseModel):
    version: int
    workspace: str = "."
    task: TaskSettings
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    run: RunSettings = Field(default_factory=RunSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    agents: dict[str, RoleAgentSettings] = Field(default_factory=dict)
    instructions: InstructionSettings = Field(default_factory=InstructionSettings)
    context: ContextDocument = Field(default_factory=ContextDocument)
    git: GitPolicySettings = Field(default_factory=GitPolicySettings)
    protection: ProtectionSettings = Field(default_factory=ProtectionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value == 1:
            raise ValueError(V1_UNSUPPORTED_MESSAGE)
        if value != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported Auto Loop config version {value}.\n"
                "This release requires the single-manifest version 2 format."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_public_manifest(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)

        limits_raw = data.get("limits")
        if limits_raw is not None:
            raise ValueError(
                "Remove top-level 'limits'; configure run limits under 'run' instead "
                "(for example run.max_turns, run.provider_retries)."
            )
        run_raw = dict(data.get("run") or {}) if isinstance(data.get("run"), dict) else {}
        if run_raw:
            data["run"] = run_raw

        models = dict(data.get("models") or {}) if isinstance(data.get("models"), dict) else {}
        agents_in = dict(data.get("agents") or {}) if isinstance(data.get("agents"), dict) else {}
        agents_out: dict[str, Any] = {}
        for role in ("planner", "worker", "reviewer"):
            agent = dict(agents_in.get(role) or {})
            if "model" in agent:
                raise ValueError(
                    f"Remove agents.{role}.model; set models.{role} instead."
                )
            if agent or role in agents_in:
                agents_out[role] = agent
        if models:
            data["models"] = models
        if agents_out:
            data["agents"] = agents_out
        return data

    @model_validator(mode="after")
    def sync_agent_roles(self) -> AutoLoopConfig:
        defaults: dict[str, AgentMode] = {
            "planner": "agent",
            "worker": "agent",
            "reviewer": "ask",
        }
        agents: dict[str, RoleAgentSettings] = {}
        for role, default_mode in defaults.items():
            incoming = self.agents.get(role) or RoleAgentSettings(mode=default_mode)
            if role == "reviewer":
                if incoming.mode != "ask":
                    raise ValueError("Reviewer agent must use ask mode")
                mode: AgentMode = "ask"
            else:
                mode = incoming.mode or default_mode
                if mode not in ("agent", "ask"):
                    raise ValueError(f"Invalid mode for {role}: {mode}")
            agents[role] = RoleAgentSettings(
                role_file=incoming.role_file,
                mode=mode,
                model=getattr(self.models, role),
            )
        object.__setattr__(self, "agents", agents)
        return self

    @property
    def limits(self) -> LimitSettings:
        return LimitSettings(**self.run.model_dump())

    @property
    def artifacts_root(self) -> str:
        return posix_rel(self.artifacts.root) or DEFAULT_ARTIFACTS_ROOT

    @property
    def task_file(self) -> str:
        return f"{self.artifacts_root}/task.md"

    @property
    def plan_file(self) -> str:
        return f"{self.artifacts_root}/plan.md"

    @property
    def reviews_dir(self) -> str:
        return f"{self.artifacts_root}/reviews"

    @property
    def event_log(self) -> str:
        return f"{self.artifacts_root}/runtime/events.jsonl"


def default_config(*, task_source: str = ".ai/proposal.md") -> AutoLoopConfig:
    return AutoLoopConfig(
        version=CONFIG_VERSION,
        workspace=".",
        task=TaskSettings(source=task_source),
        artifacts=ArtifactSettings(root=DEFAULT_ARTIFACTS_ROOT),
    )


def public_config_dict(config: AutoLoopConfig) -> dict[str, Any]:
    """Serialize the user-facing manifest shape (single source of truth per setting)."""
    data = config.model_dump(mode="json")
    data.pop("limits", None)
    agents_public: dict[str, Any] = {}
    for role, agent in config.agents.items():
        entry: dict[str, Any] = {}
        if agent.role_file:
            entry["role_file"] = agent.role_file
        default_mode: AgentMode = "ask" if role == "reviewer" else "agent"
        if agent.mode != default_mode:
            entry["mode"] = agent.mode
        if entry:
            agents_public[role] = entry
    if agents_public:
        data["agents"] = agents_public
    else:
        data.pop("agents", None)
    return data


def dump_config(config: AutoLoopConfig) -> str:
    return yaml.safe_dump(public_config_dict(config), sort_keys=False, default_flow_style=False)


def load_config(path: Path) -> AutoLoopConfig:
    raw = _load_yaml_mapping(path)
    try:
        return AutoLoopConfig.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(_format_validation_error(path, exc)) from exc


def parse_config_dict(data: dict[str, Any]) -> AutoLoopConfig:
    try:
        return AutoLoopConfig.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(_format_version_or_validation(exc)) from exc


def resolved_config_snapshot_path(artifact_root: Path) -> Path:
    return artifact_root / "runtime" / "config.resolved.yaml"


def write_resolved_config(workspace: Path, config: AutoLoopConfig) -> None:
    """Write the immutable run snapshot used on resume."""
    text = dump_config(config)
    root = artifact_root_path(workspace, config.artifacts.root).resolve()
    path = resolved_config_snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_resolved_config(artifact_root: Path) -> AutoLoopConfig:
    path = resolved_config_snapshot_path(artifact_root)
    if not path.is_file():
        raise ConfigurationError(f"Resolved run snapshot not found: {path}")
    return load_config(path)


def load_resolved_config_optional(artifact_root: Path) -> AutoLoopConfig | None:
    path = resolved_config_snapshot_path(artifact_root)
    if not path.is_file():
        return None
    return load_config(path)


def load_frozen_config(artifact_root: Path) -> AutoLoopConfig | None:
    """Load the resolved snapshot at an artifact root, if present."""
    return load_resolved_config_optional(artifact_root)


def effective_settings_snapshot(config: AutoLoopConfig) -> dict[str, Any]:
    """Comparable effective settings for round-trip tests."""
    return {
        "models": config.models.model_dump(),
        "run": config.run.model_dump(),
        "agents": {
            role: {
                "role_file": agent.role_file,
                "mode": agent.mode,
                "model": agent.model,
            }
            for role, agent in config.agents.items()
        },
        "limits": config.limits.model_dump(),
    }


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


def _format_version_or_validation(exc: Exception) -> str:
    text = str(exc)
    if "Unsupported Auto Loop config version" in text:
        if isinstance(exc, ValidationError):
            for error in exc.errors():
                message = error.get("msg", "")
                if "Unsupported Auto Loop config version" in message:
                    return message.replace("Value error, ", "")
        return text
    return str(exc)


def _format_validation_error(path: Path, exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            message = str(error.get("msg", ""))
            if "Unsupported Auto Loop config version" in message:
                return message.replace("Value error, ", "")
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
    text = str(exc)
    if "Unsupported Auto Loop config version" in text:
        return text
    return f"Invalid {path.name}\n\n{exc}\n\nFix the field above and run again."
