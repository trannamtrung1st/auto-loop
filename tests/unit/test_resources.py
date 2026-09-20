"""Packaged AI-harness resources: structure, profiles, and wheel contents."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from auto_loop.harness_pack import (
    CORE_SKILL_NAMES,
    FRONTEND_SKILL_NAMES,
    PROFILE_CORE,
    PROFILE_FRONTEND,
    harness_resources_root,
    normalize_profile,
    read_agents_md,
    read_skill_md,
    skill_names_for_profile,
    validate_pack,
)


def test_validate_pack_passes():
    validate_pack()


def test_core_profile_excludes_ui_validation():
    core = skill_names_for_profile(PROFILE_CORE)
    assert "ui-validation" not in core
    assert set(core) == set(CORE_SKILL_NAMES)


def test_frontend_profile_includes_ui_validation():
    frontend = skill_names_for_profile(PROFILE_FRONTEND)
    assert "ui-validation" in frontend
    assert set(frontend) == set(FRONTEND_SKILL_NAMES)


def test_unknown_profile_raises():
    with pytest.raises(Exception, match="unknown profile"):
        normalize_profile("mobile")


def test_agents_md_loads_and_stays_generic():
    text = read_agents_md()
    assert "minimal coherent" in text.lower()
    assert ".auto-loop/plan.md" not in text


@pytest.mark.parametrize("skill_name", FRONTEND_SKILL_NAMES)
def test_each_skill_readable_with_frontmatter(skill_name: str):
    body = read_skill_md(skill_name)
    assert body.startswith("---")
    assert "name:" in body.split("---", 2)[1]
    assert "description:" in body.split("---", 2)[1]


def test_bundled_paths_exist_via_importlib():
    root = harness_resources_root()
    assert root.joinpath("AGENTS.md").is_file()
    for name in FRONTEND_SKILL_NAMES:
        assert root.joinpath("skills", name, "SKILL.md").is_file()


def test_wheel_contains_harness_resources(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(repo_root), "--no-deps", "-w", str(wheel_dir)],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    wheels = list(wheel_dir.glob("auto_loop-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    assert any(n.endswith("auto_loop/harness_resources/AGENTS.md") for n in names)
    for skill in FRONTEND_SKILL_NAMES:
        suffix = f"auto_loop/harness_resources/skills/{skill}/SKILL.md"
        assert any(n.endswith(suffix) for n in names)
