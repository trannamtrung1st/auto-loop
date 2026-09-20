"""Install packaged AGENTS.md and Agent Skills into a target repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import as_file
from pathlib import Path

from auto_loop.harness_pack import (
    HarnessPackError,
    harness_resources_root,
    read_agents_md,
    read_skill_md,
    skill_names_for_profile,
)


@dataclass(frozen=True)
class InstallAction:
    verb: str  # create | skip
    rel_path: str
    reason: str = ""


@dataclass
class InstallResult:
    actions: list[InstallAction] = field(default_factory=list)
    bundled_agents_reference: str | None = None

    def lines(self) -> list[str]:
        out: list[str] = []
        for action in self.actions:
            if action.reason:
                out.append(f"{action.verb.upper()} {action.rel_path}: {action.reason}")
            else:
                out.append(f"{action.verb.upper()} {action.rel_path}")
        if self.bundled_agents_reference:
            out.append(f"BUNDLED_AGENTS_MD {self.bundled_agents_reference}")
        return out


def bundled_agents_md_reference_path() -> str:
    ref = harness_resources_root().joinpath("AGENTS.md")
    with as_file(ref) as path:
        return str(path.resolve())


def plan_resources_install(
    repo: Path,
    *,
    profile: str,
    no_agents_md: bool,
) -> InstallResult:
    skill_names = skill_names_for_profile(profile)
    result = InstallResult()
    agents_rel = "AGENTS.md"
    agents_path = repo / agents_rel

    if no_agents_md:
        result.actions.append(InstallAction("skip", agents_rel, "disabled by --no-agents-md"))
    elif agents_path.is_file():
        result.actions.append(InstallAction("skip", agents_rel, "already exists"))
        result.bundled_agents_reference = bundled_agents_md_reference_path()
    else:
        result.actions.append(InstallAction("create", agents_rel))

    skills_root = repo / ".agents" / "skills"
    for name in skill_names:
        rel = f".agents/skills/{name}/SKILL.md"
        dest_dir = skills_root / name
        if dest_dir.exists():
            result.actions.append(InstallAction("skip", rel, "skill directory already exists"))
            continue
        result.actions.append(InstallAction("create", rel))

    return result


def apply_resources_install(
    repo: Path,
    *,
    profile: str,
    no_agents_md: bool,
    dry_run: bool,
) -> InstallResult:
    plan = plan_resources_install(repo, profile=profile, no_agents_md=no_agents_md)
    if dry_run:
        return plan

    for action in plan.actions:
        if action.verb != "create":
            continue
        if action.rel_path == "AGENTS.md":
            target = repo / "AGENTS.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(read_agents_md(), encoding="utf-8")
            continue
        if action.rel_path.startswith(".agents/skills/") and action.rel_path.endswith("/SKILL.md"):
            parts = Path(action.rel_path).parts
            skill_name = parts[2]
            dest_dir = repo / ".agents" / "skills" / skill_name
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "SKILL.md").write_text(read_skill_md(skill_name), encoding="utf-8")

    return plan


def run_resources_install(
    repo: Path,
    *,
    profile: str,
    no_agents_md: bool,
    dry_run: bool,
) -> InstallResult:
    try:
        return apply_resources_install(
            repo,
            profile=profile,
            no_agents_md=no_agents_md,
            dry_run=dry_run,
        )
    except HarnessPackError as exc:
        raise ResourcesInstallError(str(exc)) from exc


class ResourcesInstallError(Exception):
    """Invalid install options or bundled pack."""
