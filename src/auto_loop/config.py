"""Version 2 run-manifest models and load/save helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auto_loop.context_manifest import ContextDocument
from auto_loop.exits import ExitCode
from auto_loop.paths import DEFAULT_ARTIFACTS_ROOT, posix_rel

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
    max_turns: int | None = Field(default=None, ge=1)
    max_runtime_minutes: int | None = Field(default=None, ge=1)
    max_consecutive_worker_no_progress: int | None = Field(default=None, ge=1)


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
    limits: LimitSettings = Field(default_factory=LimitSettings)
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
    def apply_convenience_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        agents_in = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        agents: dict[str, Any] = dict(agents_in)
        for role, default_mode in (("planner", "agent"), ("worker", "agent"), ("reviewer", "ask")):
            agent = dict(agents.get(role) or {})
            if role in models and isinstance(models[role], str) and models[role].strip():
                agent["model"] = models[role].strip()
            agent.setdefault("model", "auto")
            agent.setdefault("mode", default_mode)
            agent.setdefault("role_file", "")
            agents[role] = agent
        data["agents"] = agents

        run = data.get("run") if isinstance(data.get("run"), dict) else {}
        limits = dict(data.get("limits") or {}) if isinstance(data.get("limits"), dict) else {}
        if run.get("max_turns") is not None:
            limits["max_turns"] = run["max_turns"]
        if run.get("max_runtime_minutes") is not None:
            limits["max_runtime_minutes"] = run["max_runtime_minutes"]
        if run.get("max_consecutive_worker_no_progress") is not None:
            limits["max_consecutive_worker_no_progress"] = run["max_consecutive_worker_no_progress"]
        if limits:
            data["limits"] = limits
        return data

    @model_validator(mode="after")
    def default_agents(self) -> AutoLoopConfig:
        agents = dict(self.agents)
        if "planner" not in agents:
            agents["planner"] = RoleAgentSettings(model=self.models.planner, mode="agent")
        if "worker" not in agents:
            agents["worker"] = RoleAgentSettings(model=self.models.worker, mode="agent")
        if "reviewer" not in agents:
            agents["reviewer"] = RoleAgentSettings(model=self.models.reviewer, mode="ask")
        object.__setattr__(self, "agents", agents)
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


def dump_config(config: AutoLoopConfig) -> str:
    data = config.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


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
    path = resolved_config_snapshot_path(workspace / config.artifacts_root)
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


def load_config_from_repo(repo: Path) -> AutoLoopConfig:
    """Load frozen snapshot or bootstrapped `.ai/run.yaml` (tests/helpers only)."""
    from auto_loop.manifest import load_run_manifest
    from auto_loop.paths import auto_loop_root

    frozen = load_resolved_config_optional(auto_loop_root(repo))
    if frozen is not None:
        return frozen
    manifest = repo / ".ai" / "run.yaml"
    if manifest.is_file():
        return load_run_manifest(manifest).config
    raise ConfigurationError(
        "No Auto Loop configuration found.\n\n"
        "Pass a version 2 run YAML to the CLI, for example:\n"
        "  auto-loop run .ai/run.yaml"
    )


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
