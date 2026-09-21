"""Repository-root development harness structure (enhancement proposal §21)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

REQUIRED_SKILLS = (
    "auto-loop-repo-discovery",
    "auto-loop-lifecycle-change",
    "auto-loop-protocol-state",
    "auto-loop-cursor-provider",
    "auto-loop-test-and-verify",
    "auto-loop-release-review",
)

FIXED_PATHS = (
    "AGENTS.md",
    "README.md",
    "src/auto_loop/lifecycle.py",
    "src/auto_loop/loop.py",
    "src/auto_loop/models.py",
    "src/auto_loop/protocol.py",
)


def test_root_agents_md_exists():
    path = _REPO / "AGENTS.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "mechanical controller" in text.lower()
    assert ".agents/skills" in text


def test_required_skills_exist_with_unique_frontmatter():
    names: list[str] = []
    for skill in REQUIRED_SKILLS:
        skill_md = _REPO / ".agents" / "skills" / skill / "SKILL.md"
        assert skill_md.is_file(), f"missing {skill_md}"
        text = skill_md.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        assert match, f"missing frontmatter: {skill}"
        meta = yaml.safe_load(match.group(1)) or {}
        assert meta.get("name"), f"skill {skill} missing name"
        assert meta.get("description"), f"skill {skill} missing description"
        names.append(str(meta["name"]))
    assert len(names) == len(set(names))


def test_fixed_repository_paths_exist():
    for rel in FIXED_PATHS:
        assert (_REPO / rel).exists(), rel


def test_no_packaged_harness_copy():
    assert not (_REPO / "src" / "auto_loop" / "harness_resources").exists()
    assert not (_REPO / "src" / "auto_loop" / "harness_pack.py").exists()
    assert not (_REPO / "src" / "auto_loop" / "resources_install.py").exists()
    import importlib.util

    assert importlib.util.find_spec("auto_loop.harness_resources") is None
    assert importlib.util.find_spec("auto_loop.harness_pack") is None
    assert importlib.util.find_spec("auto_loop.resources_install") is None


def test_readme_points_contributors_to_root_harness():
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in readme
    assert ".agents/skills" in readme
    assert "resources install" not in readme
