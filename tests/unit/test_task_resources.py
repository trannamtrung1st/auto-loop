"""Task entry, authoritative resources, snapshots, resume, and protection."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.doctor import Severity, run_doctor
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import create_lifecycle
from auto_loop.loop import LifecycleRunner
from auto_loop.manifest import load_run_manifest
from auto_loop.product_state import is_product_tree_clean, list_product_changes, product_excludes
from auto_loop.protection import (
    ProtectionViolationError,
    assert_protected_unchanged,
    capture_protected_baseline,
)
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_inputs import RunInputError, prepare_repo_for_run
from auto_loop.run_options import RunOptions
from auto_loop.runtime import save_lifecycle_state
from auto_loop.terminal_records import CompletionRecord, save_completion_record
from tests.repo_utils import git, git_repo


def _manifest(
    repo: Path,
    *,
    task_source: str = ".ai/task.md",
    resources: list[str] | None = None,
    goal: str = "Implement the task\n",
    include_resources_key: bool = True,
) -> Path:
    yaml_path = repo / ".ai" / "run.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    task_path = repo / task_source
    task_path.parent.mkdir(parents=True, exist_ok=True)
    if not task_path.exists():
        task_path.write_text(goal if goal.endswith("\n") else f"{goal}\n", encoding="utf-8")
    resource_yaml = ""
    if include_resources_key and resources is not None:
        lines = "\n".join(f"    - {item}" for item in resources)
        resource_yaml = f"  resources:\n{lines}\n"
    yaml_path.write_text(
        "version: 2\n"
        "workspace: ..\n"
        "task:\n"
        f"  source: {task_source}\n"
        f"{resource_yaml}"
        "artifacts:\n"
        "  root: .ai/auto-loop\n"
        "context:\n"
        "  shared:\n"
        "    resources:\n"
        "      - README.md\n",
        encoding="utf-8",
    )
    readme = repo / "README.md"
    if not readme.exists():
        readme.write_text("Advisory project overview\n", encoding="utf-8")
    return yaml_path


def _write_specs(repo: Path) -> None:
    (repo / "proposal.md").write_text("spec v1\n", encoding="utf-8")
    api = repo / "requirements" / "api.md"
    api.parent.mkdir(parents=True, exist_ok=True)
    api.write_text("api v1\n", encoding="utf-8")


def _complete(repo: Path, source) -> None:
    state = create_lifecycle("abc")
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    save_completion_record(
        repo,
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w",
            reviewer_session_id="r",
            initial_base_commit=None,
            final_commit=None,
            last_approved_commit=None,
            final_review_file=".ai/auto-loop/reviews/done.md",
            task_sha256="abc",
        ),
        artifact_root=source.artifact_root,
    )


_REAL_SUBPROCESS_RUN = subprocess.run


def _cursor_subprocess(argv, *args, **kwargs):
    if argv and argv[0] == "git":
        return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
    cmd = " ".join(str(part) for part in argv)
    if "--version" in cmd:
        return subprocess.CompletedProcess(argv, 0, stdout="agent 1.0\n", stderr="")
    return subprocess.CompletedProcess(argv, 0, stdout="--resume stream-json --mode ask", stderr="")


def test_task_source_may_use_any_filename(tmp_path: Path):
    repo = git_repo(tmp_path)
    (repo / "notes").mkdir()
    (repo / "notes" / "goal.txt").write_text("Custom entry\n", encoding="utf-8")
    yaml_path = _manifest(repo, task_source="notes/goal.txt", resources=[], goal="Custom entry\n")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    snapshot = repo / ".ai" / "auto-loop" / "task.md"
    assert snapshot.is_file()
    assert "Custom entry" in snapshot.read_text(encoding="utf-8")
    assert prepared.config.task_file == ".ai/auto-loop/task.md"
    assert not (repo / ".ai" / "auto-loop" / "goal.txt").exists()


def test_manifest_with_only_task_source_still_runs(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _manifest(repo, resources=None, include_resources_key=False)
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert prepared.config.task.resources == []
    assert not (repo / ".ai" / "auto-loop" / "task-resources").exists()


def test_starter_config_recommends_task_md(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / ".ai" / "run.yaml"
    result = run_init(target)
    text = target.read_text(encoding="utf-8")
    assert "source: .ai/task.md" in text
    assert "proposal.md" not in text
    assert "task.md" in result.message
    assert default_config().task.source == ".ai/task.md"
    assert default_config().task.resources == []


def test_task_resources_default_empty_and_round_trip(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _manifest(repo, resources=None, include_resources_key=False)
    loaded = load_run_manifest(yaml_path).config
    assert loaded.task.resources == []
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md", "requirements/api.md"])
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert prepared.config.task.resources == ["proposal.md", "requirements/api.md"]


def test_invalid_task_resource_does_not_mutate_run_state(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    _complete(repo, source)
    original = (repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md").read_text(encoding="utf-8")
    (repo / "proposal.md").unlink()
    with pytest.raises(RunInputError, match="Task resource is missing or unreadable"):
        prepare_repo_for_run(load_run_manifest(yaml_path))
    assert (repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md").read_text(encoding="utf-8") == original
    assert not (repo / ".ai" / "auto-loop" / "runtime" / "archives").exists()


def test_task_resource_cannot_escape_workspace(tmp_path: Path):
    repo = git_repo(tmp_path)
    outside = tmp_path / "secret.md"
    outside.write_text("secret\n", encoding="utf-8")
    yaml_path = _manifest(repo, resources=["../secret.md"])
    with pytest.raises(RunInputError, match="Task resource escapes workspace"):
        prepare_repo_for_run(load_run_manifest(yaml_path))
    assert not (repo / ".ai" / "auto-loop").exists()

    link = repo / "linked.md"
    link.symlink_to(outside)
    yaml_path = _manifest(repo, resources=["linked.md"])
    with pytest.raises(RunInputError, match="Task resource escapes workspace"):
        prepare_repo_for_run(load_run_manifest(yaml_path))
    assert not (repo / ".ai" / "auto-loop").exists()


def test_directory_task_resource_is_rejected(tmp_path: Path):
    repo = git_repo(tmp_path)
    (repo / "docs").mkdir()
    yaml_path = _manifest(repo, resources=["docs"])
    with pytest.raises(RunInputError, match="Task resource must be a file"):
        prepare_repo_for_run(load_run_manifest(yaml_path))
    assert not (repo / ".ai" / "auto-loop").exists()


def test_new_run_snapshots_source_and_collision_safe_resources(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    inside = repo / "specs"
    inside.mkdir()
    (inside / "api.md").write_text("api v1\n", encoding="utf-8")
    (repo / "link.md").symlink_to(inside / "api.md")
    yaml_path = _manifest(
        repo,
        resources=["./proposal.md", "requirements/api.md", "link.md"],
        goal="# Task\n\nImplement `proposal.md` and see `secret-spec.md`.\n",
    )
    (repo / "secret-spec.md").write_text("not authoritative\n", encoding="utf-8")
    (repo / ".ai" / "task.md").write_text(
        "# Task\n\nImplement `proposal.md`.\n\nSee [spec](secret-spec.md).\n",
        encoding="utf-8",
    )
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    root = repo / ".ai" / "auto-loop"
    assert "Implement `proposal.md`" in (root / "task.md").read_text(encoding="utf-8")
    assert (root / "task-resources" / "proposal.md").read_text(encoding="utf-8") == "spec v1\n"
    assert (root / "task-resources" / "requirements" / "api.md").read_text(encoding="utf-8") == "api v1\n"
    assert not (root / "task-resources" / "api.md").exists()
    assert (root / "task-resources" / "link.md").read_text(encoding="utf-8") == "api v1\n"
    assert not (root / "task-resources" / "link.md").is_symlink()
    assert not (root / "task-resources" / "secret-spec.md").exists()
    assert "secret-spec.md" not in prepared.config.protection.protected_files
    assert prepared.config.task.resources == ["proposal.md", "requirements/api.md", "link.md"]


def test_new_snapshot_tree_drops_stale_files(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    stale = repo / ".ai" / "auto-loop" / "task-resources" / "old.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")
    yaml_path = _manifest(repo, resources=["proposal.md"])
    prepare_repo_for_run(load_run_manifest(yaml_path))
    root = repo / ".ai" / "auto-loop" / "task-resources"
    assert not (root / "old.md").exists()
    assert (root / "proposal.md").read_text(encoding="utf-8") == "spec v1\n"

    yaml_path = _manifest(repo, resources=[], include_resources_key=True)
    prepare_repo_for_run(load_run_manifest(yaml_path))
    assert not root.exists()


def test_archive_does_not_move_symlink_target(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    _complete(repo, source)
    snapshot = repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md"
    snapshot.unlink()
    snapshot.symlink_to(repo / "proposal.md")
    (repo / "proposal.md").write_text("spec v2\n", encoding="utf-8")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert (repo / "proposal.md").is_file()
    assert (repo / "proposal.md").read_text(encoding="utf-8") == "spec v2\n"
    frozen = repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md"
    assert frozen.is_file()
    assert not frozen.is_symlink()
    assert frozen.read_text(encoding="utf-8") == "spec v2\n"
    assert prepared.config.task.resources == ["proposal.md"]


def test_duplicate_resources_fail_before_snapshot(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md", "./proposal.md"])
    with pytest.raises(RunInputError, match="Duplicate task resource"):
        prepare_repo_for_run(load_run_manifest(yaml_path))
    assert not (repo / ".ai" / "auto-loop").exists()


def test_resume_uses_frozen_task_and_resources_after_originals_change(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md", "requirements/api.md"])
    source = load_run_manifest(yaml_path)
    first = prepare_repo_for_run(source)
    save_lifecycle_state(repo, create_lifecycle("abc"), artifact_root=source.artifact_root)
    (repo / ".ai" / "task.md").write_text("Changed entry\n", encoding="utf-8")
    (repo / "proposal.md").write_text("spec v2\n", encoding="utf-8")
    (repo / "requirements" / "api.md").write_text("api v2\n", encoding="utf-8")
    resumed = prepare_repo_for_run(load_run_manifest(yaml_path), resume=True)
    assert resumed.is_resume is True
    assert resumed.config is not None
    assert "Implement the task" in resumed.goal_text
    frozen = repo / ".ai" / "auto-loop" / "task-resources"
    assert (frozen / "proposal.md").read_text(encoding="utf-8") == "spec v1\n"
    assert (frozen / "requirements" / "api.md").read_text(encoding="utf-8") == "api v1\n"
    assert first.config.task.resources == resumed.config.task.resources


def test_resume_fails_when_frozen_task_resource_is_missing(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    save_lifecycle_state(repo, create_lifecycle("abc"), artifact_root=source.artifact_root)
    snapshot = repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md"
    snapshot.unlink()
    (repo / "proposal.md").write_text("live replacement\n", encoding="utf-8")
    with pytest.raises(RunInputError, match="will not fall back") as exc:
        prepare_repo_for_run(load_run_manifest(yaml_path), resume=True)
    assert "Frozen task resource is missing" in str(exc.value)
    assert not snapshot.exists()
    assert "live replacement" not in (repo / ".ai" / "auto-loop" / "task.md").read_text(encoding="utf-8")


def test_active_lifecycle_does_not_refresh_task_inputs(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    save_lifecycle_state(repo, create_lifecycle("abc"), artifact_root=source.artifact_root)
    (repo / "proposal.md").write_text("should not replace snapshot\n", encoding="utf-8")
    with pytest.raises(RunInputError, match="already in progress"):
        prepare_repo_for_run(load_run_manifest(yaml_path), resume=False)
    assert "spec v1" in (repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md").read_text(
        encoding="utf-8"
    )


def test_previous_task_resources_are_archived_on_new_lifecycle(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md", "requirements/api.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    _complete(repo, source)
    (repo / ".ai" / "task.md").write_text("Second task\n", encoding="utf-8")
    (repo / "proposal.md").write_text("spec v2\n", encoding="utf-8")
    (repo / "requirements" / "api.md").write_text("api v2\n", encoding="utf-8")
    yaml_path = _manifest(repo, resources=["requirements/api.md"], goal="Second task\n")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert "Second task" in prepared.goal_text
    live = repo / ".ai" / "auto-loop" / "task-resources"
    assert not (live / "proposal.md").exists()
    assert (live / "requirements" / "api.md").read_text(encoding="utf-8") == "api v2\n"
    archives = list((repo / ".ai" / "auto-loop" / "runtime" / "archives").glob("*"))
    assert len(archives) == 1
    archived = archives[0] / ".ai" / "auto-loop" / "task-resources"
    assert (archived / "proposal.md").read_text(encoding="utf-8") == "spec v1\n"
    assert (archived / "requirements" / "api.md").read_text(encoding="utf-8") == "api v1\n"


def test_protected_task_resources_and_snapshots_are_not_product_changes(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md"])
    git(repo, "add", ".")
    git(repo, "commit", "-m", "inputs")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    protected = prepared.config.protection.protected_files
    assert "proposal.md" in protected
    assert ".ai/auto-loop/task-resources/proposal.md" in protected
    assert ".ai/auto-loop/task.md" in protected
    excludes = product_excludes(prepared.config)
    assert is_product_tree_clean(repo, excludes=excludes)
    snapshot = repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md"
    snapshot.write_text("snapshot edit\n", encoding="utf-8")
    (repo / "proposal.md").write_text("live edit\n", encoding="utf-8")
    assert is_product_tree_clean(repo, excludes=excludes)
    assert list_product_changes(repo, excludes=excludes) == []
    baseline = capture_protected_baseline(repo, prepared.config)
    snapshot.write_text("snapshot edit again\n", encoding="utf-8")
    with pytest.raises(ProtectionViolationError, match="task-resources/proposal.md"):
        assert_protected_unchanged(repo, prepared.config, baseline)
    snapshot.write_text("spec v1\n", encoding="utf-8")
    baseline = capture_protected_baseline(repo, prepared.config)
    (repo / "proposal.md").write_text("another live edit\n", encoding="utf-8")
    with pytest.raises(ProtectionViolationError, match="proposal.md"):
        assert_protected_unchanged(repo, prepared.config, baseline)


def test_prompt_manifest_ranks_task_resources_above_context(tmp_path: Path):
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["proposal.md", "requirements/api.md"])
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    runner = LifecycleRunner(
        repo,
        prepared.config,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=5, verbose=False, quiet=True),
        ScriptedProvider(),
    )
    for role in ("planner", "worker", "reviewer"):
        text = runner._context_manifest(role)
        assert "AUTHORITATIVE TASK RESOURCES" in text
        assert "Frozen snapshot: .ai/auto-loop/task-resources/proposal.md" in text
        assert "Frozen snapshot: .ai/auto-loop/task-resources/requirements/api.md" in text
        assert "task.md remains the highest-level authority." in text
        assert "AVAILABLE CONTEXT" in text
        assert "authoritative over task.md or frozen task resources" in text
        assert text.index("AUTHORITATIVE TASK RESOURCES") < text.index("AVAILABLE CONTEXT")
        assert text.index("proposal.md") < text.index("README.md")
        assert "spec v1" not in text
        assert "api v1" not in text
        assert "Advisory project overview" not in text


def test_doctor_distinguishes_task_input_failures(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")
    monkeypatch.setattr("auto_loop.doctor.subprocess.run", _cursor_subprocess)
    repo = git_repo(tmp_path)
    _write_specs(repo)
    yaml_path = _manifest(repo, resources=["missing.md"])
    report = run_doctor(load_run_manifest(yaml_path))
    assert any(
        check.check_id == "task:resource" and "Task resource is missing or unreadable" in check.message
        for check in report.checks
    )
    assert any(check.check_id == "task" and check.severity == Severity.OK for check in report.checks)

    yaml_path = _manifest(repo, resources=["../secret.md"])
    report = run_doctor(load_run_manifest(yaml_path))
    assert any(
        check.check_id == "task:resource" and "escapes workspace" in check.message
        for check in report.checks
    )

    yaml_path = _manifest(repo, resources=["proposal.md"])
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    save_lifecycle_state(repo, create_lifecycle("abc"), artifact_root=source.artifact_root)
    (repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md").unlink()
    report = run_doctor(load_run_manifest(yaml_path))
    frozen = [
        check.message
        for check in report.checks
        if check.check_id == "task:frozen-resource" and check.severity == Severity.ERROR
    ]
    assert frozen
    assert any("Frozen task resource is missing" in message for message in frozen)
    assert any("will not fall back" in message for message in frozen)

    snapshot = repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md"
    snapshot.write_text("spec v1\n", encoding="utf-8")
    (repo / "proposal.md").unlink()
    report = run_doctor(load_run_manifest(yaml_path))
    assert any(
        check.check_id == "task:resource"
        and check.severity == Severity.WARNING
        and "missing or unreadable" in check.message
        for check in report.checks
    )
    assert any(
        check.check_id == "task:frozen-resource" and check.severity == Severity.OK
        for check in report.checks
    )
    assert not any(
        check.check_id == "task:resource" and check.severity == Severity.ERROR
        for check in report.checks
    )
