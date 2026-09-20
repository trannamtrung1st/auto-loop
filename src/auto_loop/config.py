"""Configuration models and load/save helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from auto_loop.exits import ExitCode

CONFIG_VERSION = 1
CONTEXT_VERSION = 1

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
    product_exclude: list[str] = Field(default_factory=lambda: [".auto-loop/**"])
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
        if not self.agents:
            object.__setattr__(
                self,
                "agents",
                {
                    "worker": RoleAgentSettings(
                        role_file=".auto-loop/agents/worker.md",
                        model="auto",
                        mode="agent",
                    ),
                    "reviewer": RoleAgentSettings(
                        role_file=".auto-loop/agents/reviewer.md",
                        model="auto",
                        mode="ask",
                    ),
                },
            )
        if not self.protection.protected_files:
            object.__setattr__(
                self,
                "protection",
                self.protection.model_copy(
                    update={
                        "protected_files": [
                            ".auto-loop/task.md",
                            ".auto-loop/config.yaml",
                            ".auto-loop/context.yaml",
                            ".auto-loop/agents/worker.md",
                            ".auto-loop/agents/reviewer.md",
                            ".auto-loop/instructions/shared.md",
                            ".auto-loop/instructions/worker.md",
                            ".auto-loop/instructions/reviewer.md",
                        ]
                    }
                ),
            )
        return self

    @model_validator(mode="after")
    def validate_agents(self) -> AutoLoopConfig:
        for role in ("worker", "reviewer"):
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
    return repo / ".auto-loop" / "config.yaml"


def load_config(path: Path) -> AutoLoopConfig:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Malformed YAML in {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"Configuration root must be a mapping: {path}")
    try:
        return AutoLoopConfig.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(str(exc)) from exc


def dump_config(config: AutoLoopConfig) -> str:
    data = config.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def load_config_from_repo(repo: Path) -> AutoLoopConfig:
    return load_config(config_path(repo))


def parse_config_dict(data: dict[str, Any]) -> AutoLoopConfig:
    try:
        return AutoLoopConfig.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(str(exc)) from exc
