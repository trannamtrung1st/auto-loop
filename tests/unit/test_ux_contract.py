"""User-facing v2 UX contract: explicit manifest, snapshots, resume, samples."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.config import ConfigurationError
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import create_lifecycle
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.manifest import load_run_locator, load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_inputs import prepare_repo_for_run
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import CompletionRecord, save_completion_record
from tests.integration.scenario_harness import git, run_opts
from tests.repo_utils import git_repo

runner = CliRunner()
_REPO = Path(__file__).resolve().parents[2]


def _write_manifest(
    repo: Path,
    *,
    yaml_rel: str = ".ai/run.yaml",
    workspace: str = "..",
    task_source: str = ".ai/proposal.md",
    artifacts_root: str = ".ai/auto-loop",
    extra: str = "",
    goal: str = "Implement X",
) -> Path:
    yaml_path = repo / yaml_rel
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    task_path = repo / task_source if not Path(task_source).is_absolute() else Path(task_source)
    if not Path(task_source).is_absolute():
        task_path.parent.mkdir(parents=True, exist_ok=True)
        if not task_path.exists():
            task_path.write_text(goal if goal.endswith("\n") else f"{goal}\n", encoding="utf-8")
    yaml_path.write_text(
        "version: 2\n"
        f"workspace: {workspace}\n"
        "task:\n"
        f"  source: {task_source}\n"
        "artifacts:\n"
        f"  root: {artifacts_root}\n"
        f"{extra}",
        encoding="utf-8",
    )
    return yaml_path


def test_fresh_init_cli_tells_user_how_to_run(tmp_path: Path, monkeypatch):
    repo = git_repo(tmp_path)
    monkeypatch.chdir(repo)
    target = repo / ".ai" / "run.yaml"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0
    assert target.is_file()
    assert not (repo / "task.md").exists()
    assert not (repo / "context.yaml").exists()
    assert "auto-loop run" in result.stdout
    assert "--goal-file" not in result.stdout


def test_task_source_is_snapshotted_into_artifact_root(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Implement X")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert "Implement X" in prepared.goal_text
    task = (repo / ".ai" / "auto-loop" / "task.md").read_text(encoding="utf-8")
    assert "Implement X" in task
    assert not (repo / "task.md").exists()
    assert (repo / ".ai" / "auto-loop" / "plan.md").is_file()
    assert (repo / ".ai" / "auto-loop" / "runtime").is_dir()
    assert not (repo / ".auto-loop").exists()


def test_arbitrary_config_filename(tmp_path: Path):
    repo = git_repo(tmp_path)
    (repo / "proposal.md").write_text("Feature work\n", encoding="utf-8")
    yaml_path = _write_manifest(
        repo,
        yaml_rel=".ai/my-feature.yml",
        workspace="..",
        task_source=".ai/../proposal.md",
        goal="Feature work",
    )
    # task_source relative to workspace
    yaml_path.write_text(
        "version: 2\nworkspace: ..\ntask:\n  source: proposal.md\nartifacts:\n  root: .ai/auto-loop\n",
        encoding="utf-8",
    )
    (repo / "proposal.md").write_text("Feature work\n", encoding="utf-8")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert "Feature work" in prepared.goal_text
    assert prepared.workspace.resolve() == repo.resolve()


def test_config_outside_workspace_uses_configured_repository(tmp_path: Path):
    repo = git_repo(tmp_path, name="product")
    (repo / "proposal.md").write_text("Outside config\n", encoding="utf-8")
    configs = tmp_path / "configs"
    configs.mkdir()
    yaml_path = configs / "product-a.yaml"
    yaml_path.write_text(
        f"version: 2\nworkspace: {repo}\ntask:\n  source: proposal.md\n"
        "artifacts:\n  root: .ai/auto-loop\n",
        encoding="utf-8",
    )
    source = load_run_manifest(yaml_path)
    assert source.workspace.resolve() == repo.resolve()
    prepared = prepare_repo_for_run(source)
    assert prepared.workspace.resolve() == repo.resolve()
    assert (repo / ".ai" / "auto-loop" / "task.md").is_file()


def test_inline_context_resources_are_validated(tmp_path: Path):
    repo = git_repo(tmp_path)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    yaml_path = _write_manifest(
        repo,
        extra="context:\n  shared:\n    resources:\n      - README.md\n",
    )
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert prepared.config.context.shared.resources[0].path == "README.md"


def test_resume_keeps_frozen_task_when_proposal_changes(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Original goal")
    prepare_repo_for_run(load_run_manifest(yaml_path))
    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    (repo / ".ai" / "proposal.md").write_text("Changed goal that must not replace the run\n", encoding="utf-8")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path), resume=True)
    assert "Original goal" in prepared.goal_text
    assert prepared.is_resume is True
    assert "Changed goal" not in (repo / ".ai" / "auto-loop" / "task.md").read_text(encoding="utf-8")


def test_run_against_active_lifecycle_is_rejected(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Original goal")
    prepare_repo_for_run(load_run_manifest(yaml_path))
    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    result = runner.invoke(app, ["run", str(yaml_path)])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    output = result.stderr + result.stdout
    assert "already in progress" in output
    assert "auto-loop resume" in output
    assert "Original goal" in (repo / ".ai" / "auto-loop" / "task.md").read_text(encoding="utf-8")


def test_v1_config_is_rejected(tmp_path: Path):
    repo = git_repo(tmp_path)
    path = repo / "auto-loop.yaml"
    path.write_text("version: 1\nmodels:\n  worker: auto\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(path)])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "version 1" in (result.stderr + result.stdout)


def test_unrelated_root_auto_loop_yaml_is_ignored(tmp_path: Path):
    repo = git_repo(tmp_path)
    (repo / "auto-loop.yaml").write_text(
        "version: 2\nworkspace: .\ntask:\n  source: decoy.md\nartifacts:\n  root: decoy-out\n",
        encoding="utf-8",
    )
    (repo / "decoy.md").write_text("decoy\n", encoding="utf-8")
    yaml_path = _write_manifest(repo, goal="Real goal")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert "Real goal" in prepared.goal_text
    assert not (repo / "decoy-out").exists()
    assert (repo / ".ai" / "auto-loop" / "task.md").is_file()


def test_missing_config_argument_fails_usage():
    result = runner.invoke(app, ["run"])
    assert result.exit_code != 0
    combined = result.stderr + result.stdout
    assert "run.yaml" in combined.lower() or "missing" in combined.lower() or "argument" in combined.lower()


def test_package_defaults_run_without_generated_agents(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Minimal valid")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert prepared.config.agents["planner"].role_file == ""
    assert prepared.config.instructions.worker.files == []
    assert not (repo / ".ai" / "auto-loop" / "agents").exists()
    assert not (repo / ".ai" / "auto-loop" / "instructions").exists()
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code in {ExitCode.LIMIT_REACHED, ExitCode.COMPLETE}


def test_kanban_sample_reaches_execution_without_manual_internal_edits(tmp_path: Path):
    sample = _REPO / "samples" / "kanban-board"
    dest = tmp_path / "kanban-board"
    shutil.copytree(sample, dest)
    git(dest, "init")
    git(dest, "config", "user.email", "t@example.com")
    git(dest, "config", "user.name", "T")
    git(dest, "add", ".")
    git(dest, "commit", "-m", "sample")
    assert not (dest / ".ai" / "auto-loop").exists()
    yaml_path = dest / ".ai" / "run.yaml"
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path))
    assert "kanban" in prepared.goal_text.lower()
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(dest, run_opts(2), provider)
    assert outcome.exit_code in {ExitCode.LIMIT_REACHED, ExitCode.COMPLETE}
    assert (dest / ".ai" / "auto-loop" / "plan.md").is_file()
    assert (dest / ".ai" / "auto-loop" / "reviews").is_dir()
    state = load_lifecycle_state(dest)
    assert state is not None
    assert state.plan_approved is True


def test_help_teaches_canonical_commands_not_internal_files():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    plain = Text.from_ansi(result.stdout).plain
    for name in ("init", "run", "status", "resume"):
        assert name in plain
    assert "migrate" not in plain
    run_help = Text.from_ansi(runner.invoke(app, ["run", "--help"]).stdout).plain
    assert "--goal-file" not in run_help
    assert "--context" not in run_help
    assert "--path" not in run_help
    assert ".ai/auto-loop/task.md" not in run_help
    init_help = Text.from_ansi(runner.invoke(app, ["init", "--help"]).stdout).plain
    assert "auto-loop.yaml" not in init_help
    status_help = Text.from_ansi(runner.invoke(app, ["status", "--help"]).stdout).plain
    resume_help = Text.from_ansi(runner.invoke(app, ["resume", "--help"]).stdout).plain
    assert "manifest" in status_help.lower() or "run config" in status_help.lower() or "yaml" in status_help.lower()
    assert "frozen" in resume_help.lower() or "stored" in resume_help.lower() or "saved" in resume_help.lower()


def test_new_run_after_completed_archives_prior(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="First goal")
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    save_completion_record(
        repo,
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w",
            reviewer_session_id="r",
            initial_base_commit=state.initial_base_commit,
            final_commit=state.last_approved_commit,
            last_approved_commit=state.last_approved_commit,
            final_review_file=".ai/auto-loop/reviews/done.md",
            task_sha256="abc",
        ),
        artifact_root=source.artifact_root,
    )
    (repo / ".ai" / "proposal.md").write_text("Second goal\n", encoding="utf-8")
    prepared = prepare_repo_for_run(load_run_manifest(yaml_path), resume=False)
    assert prepared.is_resume is False
    assert "Second goal" in prepared.goal_text
    archives = list((repo / ".ai" / "auto-loop" / "runtime" / "archives").glob("*"))
    assert archives
    assert "Second goal" in (repo / ".ai" / "auto-loop" / "task.md").read_text(encoding="utf-8")


def test_artifact_containment_rejects_escape(tmp_path: Path):
    repo = git_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    yaml_path = repo / "run.yaml"
    yaml_path.write_text(
        f"version: 2\nworkspace: .\ntask:\n  source: proposal.md\nartifacts:\n  root: {outside}\n",
        encoding="utf-8",
    )
    (repo / "proposal.md").write_text("goal\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(yaml_path)])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    assert "artifacts.root" in (result.stderr + result.stdout)
    assert not (outside / "runtime").exists()


def test_operational_commands_survive_corrupt_manifest_fields(tmp_path: Path, monkeypatch):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Active goal")
    prepare_repo_for_run(load_run_manifest(yaml_path))
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state, artifact_root=repo / ".ai" / "auto-loop")
    yaml_path.write_text(
        "version: 2\nworkspace: ..\ntask:\n  source: /no/such/file.md\n"
        "artifacts:\n  root: .ai/auto-loop\nrun:\n  max_turns: not-a-number\n",
        encoding="utf-8",
    )

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a[0], 0, stdout="--resume stream-json ask", stderr="")

    monkeypatch.setattr("auto_loop.doctor.subprocess.run", fake_run)
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")

    status = runner.invoke(app, ["status", str(yaml_path)])
    assert status.exit_code == 0
    assert "lifecycle:" in status.stdout

    logs = runner.invoke(app, ["logs", str(yaml_path)])
    assert logs.exit_code == 0

    from auto_loop.loop import RunOutcome

    def fake_run_lifecycle(*_args, **_kwargs):
        return RunOutcome(
            exit_code=ExitCode.COMPLETE,
            state=load_lifecycle_state(repo, repo / ".ai" / "auto-loop"),
            message="",
        )

    monkeypatch.setattr("auto_loop.cli.run_lifecycle", fake_run_lifecycle)
    resumed = runner.invoke(app, ["resume", str(yaml_path), "--quiet"])
    assert resumed.exit_code == int(ExitCode.COMPLETE)
    assert "Invalid" not in (resumed.stderr + resumed.stdout)


def test_locator_rejects_non_mapping_artifacts_section(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = repo / ".ai" / "run.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text(
        "version: 2\nworkspace: ..\nartifacts: not-a-mapping\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="artifacts must be a mapping"):
        load_run_locator(yaml_path)


def test_blocked_idempotent_before_missing_context_resources(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(
        repo,
        extra="context:\n  shared:\n    resources:\n      - README.md\n",
        goal="Blocked goal",
    )
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    state = create_lifecycle(head_commit(repo))
    from auto_loop.lifecycle import LifecycleStatus
    from auto_loop.terminal_records import BlockedRecord, save_blocked_record
    from datetime import datetime, timezone

    state.status = LifecycleStatus.BLOCKED
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    save_blocked_record(
        repo,
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=state.turn,
            worker_session_id="w",
            reviewer_session_id="r",
            summary="external blocker",
        ),
        artifact_root=source.artifact_root,
    )
    (repo / "README.md").unlink()
    outcome = run_lifecycle(repo, run_opts(1), ScriptedProvider())
    assert outcome.exit_code == ExitCode.BLOCKED


def test_stale_terminal_record_does_not_mark_active_run_terminal(tmp_path: Path):
    repo = git_repo(tmp_path)
    yaml_path = _write_manifest(repo, goal="Second run")
    source = load_run_manifest(yaml_path)
    prepare_repo_for_run(source)
    state_b = create_lifecycle(head_commit(repo), lifecycle_id="lifecycle-b")
    save_lifecycle_state(repo, state_b, artifact_root=source.artifact_root)
    from auto_loop.terminal_records import CompletionRecord, save_completion_record
    from datetime import datetime, timezone

    save_completion_record(
        repo,
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id="lifecycle-a",
            turn=1,
            worker_session_id="w",
            reviewer_session_id="r",
            initial_base_commit=head_commit(repo),
            final_commit=head_commit(repo),
            last_approved_commit=head_commit(repo),
            final_review_file=".ai/auto-loop/reviews/0001-final.md",
            task_sha256="abc",
        ),
        artifact_root=source.artifact_root,
    )
    from auto_loop.run_inputs import has_active_lifecycle, has_terminal_record

    assert has_active_lifecycle(repo, source.artifact_root)
    assert not has_terminal_record(repo, source.artifact_root)


def test_absolute_in_workspace_artifact_root_excluded_from_product_tree(tmp_path: Path):
    repo = git_repo(tmp_path)
    state_dir = repo / ".state" / "auto-loop"
    yaml_path = repo / "run.yaml"
    yaml_path.write_text(
        f"version: 2\nworkspace: .\ntask:\n  source: proposal.md\n"
        f"artifacts:\n  root: {state_dir.resolve()}\n",
        encoding="utf-8",
    )
    (repo / "proposal.md").write_text("goal\n", encoding="utf-8")
    source = load_run_manifest(yaml_path)
    assert source.config.artifacts.root == ".state/auto-loop"
    prepared = prepare_repo_for_run(source)
    from auto_loop.product_state import is_product_tree_clean, product_excludes

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "plan.md").write_text("plan\n", encoding="utf-8")
    assert is_product_tree_clean(repo, excludes=product_excludes(prepared.config))


def test_symlink_in_workspace_artifact_root_excluded_from_product_tree(tmp_path: Path):
    repo = git_repo(tmp_path)
    state_dir = repo / ".state" / "auto-loop"
    state_dir.mkdir(parents=True)
    link = repo / "loop-state"
    link.symlink_to(state_dir, target_is_directory=True)
    yaml_path = repo / "run.yaml"
    yaml_path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: proposal.md\n"
        "artifacts:\n  root: loop-state\n",
        encoding="utf-8",
    )
    (repo / "proposal.md").write_text("goal\n", encoding="utf-8")
    source = load_run_manifest(yaml_path)
    assert source.config.artifacts.root == ".state/auto-loop"
    prepared = prepare_repo_for_run(source)
    from auto_loop.product_state import is_product_tree_clean, product_excludes

    (state_dir / "plan.md").write_text("plan\n", encoding="utf-8")
    assert is_product_tree_clean(repo, excludes=product_excludes(prepared.config))
