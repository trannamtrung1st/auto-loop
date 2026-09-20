"""resources install behavior: profiles, conflicts, dry-run, and wheel sources."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.harness_pack import read_agents_md, read_skill_md
from auto_loop.resources_install import apply_resources_install, plan_resources_install

runner = CliRunner()


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            h.update(rel.encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def test_clean_install_bytes_match_packaged_sources(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    apply_resources_install(repo, profile="frontend", no_agents_md=False, dry_run=False)
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == read_agents_md()
    for skill in (
        "repo-discovery",
        "implementation-batch",
        "test-and-verify",
        "debug-failure",
        "review-evidence",
        "ui-validation",
    ):
        dest = repo / ".agents" / "skills" / skill / "SKILL.md"
        assert dest.read_text(encoding="utf-8") == read_skill_md(skill)


def test_core_profile_skips_ui_validation(tmp_path: Path):
    plan = plan_resources_install(tmp_path, profile="core", no_agents_md=False)
    paths = [a.rel_path for a in plan.actions if a.verb == "create"]
    assert not any("ui-validation" in p for p in paths)
    assert any("review-evidence" in p for p in paths)


def test_existing_agents_md_and_skill_unchanged(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = repo / "AGENTS.md"
    agents.write_text("custom agents\n", encoding="utf-8")
    skill_dir = repo / ".agents" / "skills" / "repo-discovery"
    skill_dir.mkdir(parents=True)
    existing_skill = skill_dir / "SKILL.md"
    existing_skill.write_text("keep me\n", encoding="utf-8")
    other_skill = repo / ".agents" / "skills" / "other-skill"
    other_skill.mkdir(parents=True)
    (other_skill / "SKILL.md").write_text("untouched\n", encoding="utf-8")

    result = apply_resources_install(repo, profile="core", no_agents_md=False, dry_run=False)
    assert agents.read_text(encoding="utf-8") == "custom agents\n"
    assert existing_skill.read_text(encoding="utf-8") == "keep me\n"
    assert (other_skill / "SKILL.md").read_text(encoding="utf-8") == "untouched\n"
    skip_agents = [a for a in result.actions if a.rel_path == "AGENTS.md"][0]
    assert skip_agents.verb == "skip"
    assert result.bundled_agents_reference
    assert Path(result.bundled_agents_reference).is_file()


def _action_lines(actions) -> list[str]:
    return [
        f"{a.verb.upper()} {a.rel_path}" + (f": {a.reason}" if a.reason else "")
        for a in actions
    ]


@pytest.mark.parametrize(
    ("profile", "no_agents_md"),
    [
        ("core", False),
        ("frontend", False),
        ("core", True),
    ],
)
def test_dry_run_predicts_install_and_is_non_destructive(
    tmp_path: Path, profile: str, no_agents_md: bool
):
    repo = tmp_path / "repo"
    repo.mkdir()
    before = _tree_digest(repo)
    expected = plan_resources_install(repo, profile=profile, no_agents_md=no_agents_md)
    dry_result = apply_resources_install(
        repo, profile=profile, no_agents_md=no_agents_md, dry_run=True
    )
    assert _action_lines(dry_result.actions) == _action_lines(expected.actions)
    assert _tree_digest(repo) == before

    apply_resources_install(repo, profile=profile, no_agents_md=no_agents_md, dry_run=False)
    for action in expected.actions:
        if action.verb != "create":
            continue
        assert (repo / action.rel_path).is_file()

    after_plan = plan_resources_install(repo, profile=profile, no_agents_md=no_agents_md)
    for action in after_plan.actions:
        if action.rel_path == "AGENTS.md" and no_agents_md:
            assert action.verb == "skip"
        elif action.rel_path == "AGENTS.md":
            assert action.verb == "skip" and action.reason
        else:
            assert action.verb == "skip"


def test_wheel_installed_package_serves_install_bytes(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheels"
    venv_dir = tmp_path / "venv"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(repo_root), "--no-deps", "-w", str(wheel_dir)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    pip = venv_dir / "bin" / "pip"
    wheel = next(wheel_dir.glob("auto_loop-*.whl"))
    subprocess.run([str(pip), "install", str(wheel)], check=True, capture_output=True)
    py = venv_dir / "bin" / "python"
    script = """
import sys
from pathlib import Path
from auto_loop.resources_install import apply_resources_install
from auto_loop.harness_pack import read_agents_md, read_skill_md
repo = Path(sys.argv[1])
apply_resources_install(repo, profile="core", no_agents_md=False, dry_run=False)
assert (repo / "AGENTS.md").read_text(encoding="utf-8") == read_agents_md()
assert (repo / ".agents/skills/repo-discovery/SKILL.md").read_text(encoding="utf-8") == read_skill_md("repo-discovery")
"""
    target = tmp_path / "target"
    target.mkdir()
    proc = subprocess.run(
        [str(py), "-c", script, str(target)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_cli_frontend_dry_run_lists_ui_validation(tmp_path: Path):
    result = runner.invoke(
        app,
        ["resources", "install", str(tmp_path), "--profile", "frontend", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "CREATE .agents/skills/ui-validation/SKILL.md" in result.stdout
