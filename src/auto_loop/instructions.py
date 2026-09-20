"""Protocol and role instruction composition."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from auto_loop.config import AutoLoopConfig, InstructionMode

REPOSITORY_GUIDANCE_REMINDER = (
    "Follow applicable repository-level agent instructions and skills in addition to "
    "the auto-loop role instructions. If they conflict with task.md or the auto-loop "
    "protocol contract, task.md/protocol wins as appropriate."
)


def _read_template(relative: str) -> str:
    root = resources.files("auto_loop").joinpath("templates")
    return root.joinpath(relative).read_text(encoding="utf-8").strip()


def load_protocol_contract(role: str) -> str:
    shared = _read_template("protocol/shared.md")
    role_path = f"protocol/{role}.md"
    role_text = _read_template(role_path)
    return f"{shared}\n\n{role_text}"


def _section(title: str, body: str) -> str:
    return f"===== {title} =====\n{body.strip()}"


def _read_repo_file(repo: Path, rel: str) -> str:
    path = repo / rel
    return path.read_text(encoding="utf-8").strip()


def collect_custom_instruction_paths(config: AutoLoopConfig, role: str) -> list[str]:
    paths: list[str] = []
    paths.extend(config.instructions.shared.files)
    if role == "worker":
        paths.extend(config.instructions.worker.files)
    elif role == "reviewer":
        paths.extend(config.instructions.reviewer.files)
    return paths


def validate_custom_instruction_files(repo: Path, config: AutoLoopConfig) -> list[str]:
    """Return actionable errors for configured custom instruction paths."""
    errors: list[str] = []
    seen: set[str] = set()
    for role in ("worker", "reviewer"):
        mode = config.instructions.worker.mode if role == "worker" else config.instructions.reviewer.mode
        if mode not in ("extend", "replace_role"):
            errors.append(f"Invalid instruction mode for {role}: {mode}")
    for rel in collect_custom_instruction_paths(config, "worker") + collect_custom_instruction_paths(
        config, "reviewer"
    ):
        if rel in seen:
            continue
        seen.add(rel)
        path = repo / rel
        if not path.is_file():
            errors.append(f"Configured instruction file missing or not readable: {rel}")
    return errors


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

    mode: InstructionMode = (
        config.instructions.worker.mode if role == "worker" else config.instructions.reviewer.mode
    )
    if mode == "extend":
        playbook_path = config.agents[role].role_file
        sections.append(_section("AUTO_LOOP_ROLE_PLAYBOOK", _read_repo_file(repo, playbook_path)))

    for rel in config.instructions.shared.files:
        sections.append(_section(f"ADVISORY_SHARED:{rel}", _read_repo_file(repo, rel)))
    role_files = (
        config.instructions.worker.files if role == "worker" else config.instructions.reviewer.files
    )
    for rel in role_files:
        sections.append(_section(f"ADVISORY_{role.upper()}:{rel}", _read_repo_file(repo, rel)))

    sections.append(_section("REPOSITORY_GUIDANCE", REPOSITORY_GUIDANCE_REMINDER))
    return "\n\n".join(sections)
