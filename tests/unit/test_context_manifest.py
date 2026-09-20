"""Context manifest validation and prompt append tests."""

import subprocess
from pathlib import Path

import yaml

from auto_loop.context_manifest import (
    ContextDocument,
    ManifestEntry,
    RoleManifestSection,
    load_context_file,
    render_resource_manifest,
    validate_context,
)
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import create_lifecycle
from auto_loop.prompts import TurnContext, build_worker_prompt


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    return repo


def test_role_filtered_manifest_and_dedupe(tmp_path: Path):
    doc = ContextDocument(
        shared=RoleManifestSection(
            resources=[ManifestEntry(path="docs/a.md", purpose="shared doc")],
        ),
        worker=RoleManifestSection(
            resources=[ManifestEntry(path="src/w.py", purpose="worker only")],
        ),
        reviewer=RoleManifestSection(
            resources=[ManifestEntry(path="src/r.py", purpose="reviewer only")],
        ),
    )
    worker = render_resource_manifest(doc, "worker")
    reviewer = render_resource_manifest(doc, "reviewer")
    assert "docs/a.md" in worker and "src/w.py" in worker
    assert "src/r.py" not in worker
    assert "src/r.py" in reviewer
    assert "src/w.py" not in reviewer


def test_required_missing_is_error_optional_is_warning(tmp_path: Path):
    repo = _repo(tmp_path)
    doc = ContextDocument(
        shared=RoleManifestSection(
            resources=[
                ManifestEntry(path="missing-required.md", purpose="x", required=True),
                ManifestEntry(path="missing-optional.md", purpose="y", required=False),
            ]
        )
    )
    result = validate_context(repo, doc)
    assert not result.ok_for_run
    assert any("required" in issue.message and issue.severity == "error" for issue in result.issues)
    assert any("optional" in issue.message and issue.severity == "warning" for issue in result.issues)


def test_path_outside_workspace_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    doc = ContextDocument(
        shared=RoleManifestSection(
            resources=[ManifestEntry(path="../outside.md", purpose="bad")],
        )
    )
    result = validate_context(repo, doc)
    assert any("escapes workspace" in issue.message for issue in result.issues)


def test_skill_frontmatter_validation(tmp_path: Path):
    repo = _repo(tmp_path)
    skill = repo / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: when needed\n---\n", encoding="utf-8")
    doc = ContextDocument(
        worker=RoleManifestSection(
            skills=[ManifestEntry(path=".agents/skills/demo", purpose="demo skill")],
        )
    )
    result = validate_context(repo, doc)
    assert result.ok_for_run


def test_manifest_appended_on_resume_turn(tmp_path: Path):
    state = create_lifecycle("abc")
    manifest = render_resource_manifest(ContextDocument(), "worker")
    ctx = TurnContext(
        task_path=".auto-loop/task.md",
        plan_path=".auto-loop/plan.md",
        latest_review_path=None,
        head_commit="abc",
        product_clean=True,
        resource_manifest=manifest,
    )
    prompt = build_worker_prompt(state, ctx)
    assert "AVAILABLE TASK RESOURCES" in prompt


def test_load_init_context_file(tmp_path: Path):
    repo = _repo(tmp_path)
    run_init(repo)
    doc = load_context_file(repo / ".auto-loop" / "context.yaml")
    assert doc.version == 1
