"""Bundled cross-agent harness resources (AGENTS.md and Agent Skills)."""

from __future__ import annotations

import re
from importlib import resources
from typing import Final

PROFILE_CORE: Final = "core"
PROFILE_FRONTEND: Final = "frontend"

CORE_SKILL_NAMES: Final[tuple[str, ...]] = (
    "repo-discovery",
    "implementation-batch",
    "test-and-verify",
    "debug-failure",
    "review-evidence",
)

FRONTEND_SKILL_NAMES: Final[tuple[str, ...]] = CORE_SKILL_NAMES + ("ui-validation",)

_PROFILE_SKILLS: Final[dict[str, tuple[str, ...]]] = {
    PROFILE_CORE: CORE_SKILL_NAMES,
    PROFILE_FRONTEND: FRONTEND_SKILL_NAMES,
}

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)


class HarnessPackError(ValueError):
    """Invalid profile or bundled resource layout."""


def normalize_profile(profile: str) -> str:
    key = profile.strip().lower()
    if key not in _PROFILE_SKILLS:
        raise HarnessPackError(f"unknown profile: {profile!r} (expected core or frontend)")
    return key


def skill_names_for_profile(profile: str) -> tuple[str, ...]:
    return _PROFILE_SKILLS[normalize_profile(profile)]


def harness_resources_root():
    return resources.files("auto_loop").joinpath("harness_resources")


def read_agents_md() -> str:
    return harness_resources_root().joinpath("AGENTS.md").read_text(encoding="utf-8")


def read_skill_md(skill_name: str) -> str:
    path = harness_resources_root().joinpath("skills", skill_name, "SKILL.md")
    return path.read_text(encoding="utf-8")


def parse_skill_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise HarnessPackError("SKILL.md missing YAML frontmatter delimiters")
    block = match.group(1)
    fields: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def validate_skill(skill_name: str, text: str) -> None:
    fm = parse_skill_frontmatter(text)
    if not fm.get("name"):
        raise HarnessPackError(f"{skill_name}: frontmatter missing name")
    if not fm.get("description"):
        raise HarnessPackError(f"{skill_name}: frontmatter missing description")
    if fm["name"] != skill_name:
        raise HarnessPackError(
            f"{skill_name}: frontmatter name {fm['name']!r} does not match directory"
        )
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    if len(body) < 80:
        raise HarnessPackError(f"{skill_name}: skill body too short to be useful")


def validate_pack() -> None:
    """Raise HarnessPackError if bundled resources fail structure checks."""
    root = harness_resources_root()
    agents = root.joinpath("AGENTS.md")
    if not agents.is_file():
        raise HarnessPackError("missing harness_resources/AGENTS.md")
    agents_text = agents.read_text(encoding="utf-8")
    if len(agents_text.strip()) < 100:
        raise HarnessPackError("AGENTS.md too short")
    lowered = agents_text.lower()
    if ".auto-loop/plan.md" in lowered or "worker `complete`" in lowered:
        raise HarnessPackError("AGENTS.md must not duplicate auto-loop lifecycle protocol")

    skills_root = root.joinpath("skills")
    for name in FRONTEND_SKILL_NAMES:
        skill_path = skills_root.joinpath(name, "SKILL.md")
        if not skill_path.is_file():
            raise HarnessPackError(f"missing skill: {name}/SKILL.md")
        validate_skill(name, skill_path.read_text(encoding="utf-8"))

    for name in CORE_SKILL_NAMES:
        if name not in {p.name for p in skills_root.iterdir() if p.is_dir()}:
            raise HarnessPackError(f"core skill directory missing: {name}")
