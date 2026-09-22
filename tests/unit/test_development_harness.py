"""Repository-root contributor harness contract (not runtime)."""

from __future__ import annotations

import importlib.util
import re
import zipfile
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SKILLS_ROOT = _REPO / ".agents" / "skills"

FIXED_PATHS = (
    "AGENTS.md",
    "README.md",
    "src/auto_loop/lifecycle.py",
    "src/auto_loop/loop.py",
    "src/auto_loop/models.py",
    "src/auto_loop/protocol.py",
)


def _discover_contributor_skills() -> list[Path]:
    return sorted(_SKILLS_ROOT.glob("*/SKILL.md"))


def test_root_agents_md_describes_contributor_harness():
    path = _REPO / "AGENTS.md"
    text = path.read_text(encoding="utf-8").lower()
    assert "contributor" in text or "developing auto-loop" in text
    assert "not part of the" in text or "not copied" in text or "not packaged" in text
    assert ".agents/skills" in text
    assert "mechanical controller" in text


def test_harness_requires_explicit_request_for_full_test_suite():
    guidance_paths = (
        _REPO / "AGENTS.md",
        _SKILLS_ROOT / "auto-loop-develop" / "SKILL.md",
        _SKILLS_ROOT / "auto-loop-review" / "SKILL.md",
    )
    for path in guidance_paths:
        text = path.read_text(encoding="utf-8").lower()
        assert "focused" in text, path
        assert "full offline" in text, path
        assert "explicitly request" in text, path


def test_contributor_skills_exist_with_valid_frontmatter():
    skill_files = _discover_contributor_skills()
    assert len(skill_files) >= 2, "expected at least develop + review workflow skills"
    names: list[str] = []
    for skill_md in skill_files:
        text = skill_md.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        assert match, f"missing frontmatter: {skill_md}"
        meta = yaml.safe_load(match.group(1)) or {}
        assert meta.get("name"), f"missing name: {skill_md}"
        assert meta.get("description"), f"missing description: {skill_md}"
        names.append(str(meta["name"]))
    assert len(names) == len(set(names))


def test_fixed_repository_paths_exist():
    for rel in FIXED_PATHS:
        assert (_REPO / rel).exists(), rel


def test_no_packaged_harness_copy():
    assert not (_REPO / "src" / "auto_loop" / "harness_resources").exists()
    assert not (_REPO / "src" / "auto_loop" / "harness_pack.py").exists()
    assert not (_REPO / "src" / "auto_loop" / "resources_install.py").exists()
    assert importlib.util.find_spec("auto_loop.harness_resources") is None
    assert importlib.util.find_spec("auto_loop.harness_pack") is None
    assert importlib.util.find_spec("auto_loop.resources_install") is None


def test_runtime_templates_do_not_embed_repo_development_skills():
    contributor_skill_names = ("auto-loop-develop", "auto-loop-review")
    templates = _REPO / "src" / "auto_loop" / "templates"
    for path in templates.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for name in contributor_skill_names:
            assert name not in text
        assert ".agents/skills/" not in text


def test_readme_points_contributors_to_root_harness():
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in readme
    assert ".agents/skills" in readme
    assert "resources install" not in readme


def test_wheel_excludes_repository_development_harness(tmp_path: Path):
    import subprocess
    import sys

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(_REPO), "--no-deps", "-w", str(wheel_dir)],
        capture_output=True,
        text=True,
        cwd=_REPO,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    wheels = list(wheel_dir.glob("auto_loop-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert not any(".agents/" in n or "AGENTS.md" in n for n in names)
    assert not any("harness_resources" in n for n in names)
