"""Protocol and role instruction composition from package defaults plus optional files."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from auto_loop.config import AutoLoopConfig, InstructionMode, InstructionRoleSettings

REPOSITORY_GUIDANCE_REMINDER = (
    "Follow applicable repository-level agent instructions and skills in addition to "
    "the auto-loop role instructions. If they conflict with task.md, frozen task "
    "resources, or the auto-loop protocol contract, task.md and the protocol win; "
    "frozen task resources outrank advisory context."
)


def _read_template(relative: str) -> str:
    root = resources.files("auto_loop").joinpath("templates")
    return root.joinpath(relative).read_text(encoding="utf-8").strip()


def load_protocol_contract(role: str) -> str:
    shared = _read_template("protocol/shared.md")
    role_path = f"protocol/{role}.md"
    role_text = _read_template(role_path)
    return f"{shared}\n\n{role_text}"


def load_role_playbook(role: str) -> str:
    return _read_template(f"agents/{role}.md")


def _section(title: str, body: str) -> str:
    return f"===== {title} =====\n{body.strip()}"


def _read_repo_file(repo: Path, rel: str) -> str:
    path = repo / rel
    return path.read_text(encoding="utf-8").strip()


def _role_instruction_settings(config: AutoLoopConfig, role: str) -> InstructionRoleSettings:
    if role == "planner":
        return config.instructions.planner
    if role == "worker":
        return config.instructions.worker
    if role == "reviewer":
        return config.instructions.reviewer
    raise ValueError(f"Unknown instruction role: {role}")


def collect_custom_instruction_paths(config: AutoLoopConfig, role: str) -> list[str]:
    paths: list[str] = []
    paths.extend(config.instructions.shared.files)
    paths.extend(_role_instruction_settings(config, role).files)
    return paths


def validate_custom_instruction_files(repo: Path, config: AutoLoopConfig) -> list[str]:
    """Return actionable errors for configured custom instruction and role-file paths."""
    errors: list[str] = []
    seen: set[str] = set()
    for role in ("planner", "worker", "reviewer"):
        mode = _role_instruction_settings(config, role).mode
        if mode not in ("extend", "replace_role"):
            errors.append(f"Invalid instruction mode for {role}: {mode}")
        role_file = config.agents[role].role_file
        if role_file and role_file not in seen:
            seen.add(role_file)
            path = repo / role_file
            if not path.is_file():
                errors.append(f"Configured role file missing or not readable: {role_file}")
    for role in ("planner", "worker", "reviewer"):
        for rel in collect_custom_instruction_paths(config, role):
            if rel in seen:
                continue
            seen.add(rel)
            path = repo / rel
            if not path.is_file():
                errors.append(f"Configured instruction file missing or not readable: {rel}")
    return errors


def _playbook_text(repo: Path, config: AutoLoopConfig, role: str) -> str:
    role_file = config.agents[role].role_file
    if role_file:
        return _read_repo_file(repo, role_file)
    return load_role_playbook(role)


def compose_role_instructions(
    repo: Path,
    config: AutoLoopConfig,
    role: str,
    *,
    first_invocation: bool,
) -> str:
    """Assemble protocol, playbook, and custom guidance for the first role session turn."""
    if not first_invocation:
        return ""

    sections: list[str] = []
    protocol = load_protocol_contract(role)
    sections.append(_section("AUTO_LOOP_PROTOCOL", protocol))

    settings = _role_instruction_settings(config, role)
    mode: InstructionMode = settings.mode
    if mode == "extend":
        sections.append(_section("AUTO_LOOP_ROLE_PLAYBOOK", _playbook_text(repo, config, role)))

    for rel in config.instructions.shared.files:
        sections.append(_section(f"ADVISORY_SHARED:{rel}", _read_repo_file(repo, rel)))
    for rel in settings.files:
        sections.append(_section(f"ADVISORY_{role.upper()}:{rel}", _read_repo_file(repo, rel)))

    sections.append(_section("REPOSITORY_GUIDANCE", REPOSITORY_GUIDANCE_REMINDER))
    return "\n\n".join(sections)
