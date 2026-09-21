"""User-facing UX contract: init, goal inputs, resume, legacy, samples, help."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from auto_loop.cli import app
from auto_loop.config import load_config_from_repo
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import bootstrap_workspace, run_init
from auto_loop.lifecycle import create_lifecycle
from auto_loop.loop import run_lifecycle
from auto_loop.migrate_cmd import migrate_legacy_layout
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_inputs import RunInputError, prepare_repo_for_run, RunInputs
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from tests.integration.scenario_harness import git, run_opts
from tests.repo_utils import git_repo

runner = CliRunner()
_REPO = Path(__file__).resolve().parents[2]


def test_fresh_init_cli_tells_user_how_to_run(tmp_path: Path):
    repo = git_repo(tmp_path)
    result = runner.invoke(app, ["init", str(repo)])
    assert result.exit_code == 0
    assert (repo / "auto-loop.yaml").is_file()
    assert not (repo / "task.md").exists()
    assert not (repo / "context.yaml").exists()
    assert "auto-loop run" in result.stdout
    assert "--goal-file" in result.stdout


def test_inline_goal_becomes_canonical_snapshot(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepared = prepare_repo_for_run(repo, RunInputs(goal_text="Implement X"))
    assert "Implement X" in prepared.goal_text
    task = (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")
    assert "Implement X" in task
    assert not (repo / "task.md").exists()
    assert (repo / ".auto-loop" / "plan.md").is_file()
    assert (repo / ".auto-loop" / "runtime").is_dir()


def test_goal_file_becomes_run_goal(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    (repo / "goal.md").write_text("# Board\n\nBuild a kanban board.\n", encoding="utf-8")
    prepared = prepare_repo_for_run(repo, RunInputs(goal_file=Path("goal.md")))
    assert "Build a kanban board." in prepared.goal_text
    assert "Build a kanban board." in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")


def test_explicit_context_is_optional_and_snapshotted(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    (repo / "goal.md").write_text("Implement X\n", encoding="utf-8")
    context = repo / "extra-context.yaml"
    context.write_text(
        "version: 1\nshared:\n  resources: []\n  skills: []\n"
        "planner:\n  resources: []\n  skills: []\n"
        "worker:\n  resources: []\n  skills: []\n"
        "reviewer:\n  resources: []\n  skills: []\n",
        encoding="utf-8",
    )
    prepare_repo_for_run(
        repo,
        RunInputs(goal_file=Path("goal.md"), context_file=context),
    )
    assert (repo / ".auto-loop" / "context.yaml").is_file()
    assert not (repo / "context.yaml").exists()


def test_resume_keeps_stored_goal_when_root_goal_changes(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Original goal"))
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    (repo / "goal.md").write_text("Changed goal that must not replace the run\n", encoding="utf-8")
    prepared = prepare_repo_for_run(repo, RunInputs(resume_only=True))
    assert "Original goal" in prepared.goal_text
    assert prepared.is_resume is True
    assert "Changed goal" not in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")


def test_new_goal_does_not_overwrite_existing_run(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Original goal"))
    from auto_loop.git import head_commit

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    result = runner.invoke(app, ["run", "--path", str(repo), "A different goal"])
    assert result.exit_code == int(ExitCode.CONFIG_ERROR)
    output = result.stderr + result.stdout
    assert "already in progress" in output
    assert "auto-loop resume" in output
    assert "Original goal" in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")


def test_legacy_project_is_loadable_and_migration_is_non_destructive(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, goal="Legacy stored goal")
    (repo / "auto-loop.yaml").unlink()
    from auto_loop.config import config_path, dump_config, load_config, resolved_config_snapshot_path

    frozen = resolved_config_snapshot_path(repo)
    if frozen.is_file():
        config_path(repo).parent.mkdir(parents=True, exist_ok=True)
        config_path(repo).write_text(dump_config(load_config(frozen)), encoding="utf-8")
    (repo / "task.md").write_text("Deprecated root task\n", encoding="utf-8")
    (repo / "context.yaml").write_text("version: 1\n", encoding="utf-8")
    from auto_loop.git import head_commit

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    cfg = load_config_from_repo(repo)
    assert cfg.agents["worker"].mode == "ask" or cfg.agents["reviewer"].mode == "ask"
    result = migrate_legacy_layout(repo)
    assert result.migrated is True
    assert (repo / "auto-loop.yaml").is_file()
    assert load_lifecycle_state(repo) is not None
    assert (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8").startswith("Legacy stored goal")
    assert (repo / "task.md").is_file()
    assert "task.md" in "\n".join(result.deprecated_inputs)


def test_kanban_sample_reaches_execution_without_manual_internal_edits(tmp_path: Path):
    sample = _REPO / "samples" / "kanban-board"
    dest = tmp_path / "kanban-board"
    shutil.copytree(sample, dest)
    git(dest, "init")
    git(dest, "config", "user.email", "t@example.com")
    git(dest, "config", "user.name", "T")
    git(dest, "add", ".")
    git(dest, "commit", "-m", "sample")
    assert not (dest / ".auto-loop").exists()
    prepared = prepare_repo_for_run(dest, RunInputs(goal_file=dest / "goal.md"))
    assert "kanban" in prepared.goal_text.lower()
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(dest, run_opts(2), provider, inputs=RunInputs())
    assert outcome.exit_code in {ExitCode.LIMIT_REACHED, ExitCode.COMPLETE}
    assert (dest / ".auto-loop" / "plan.md").is_file()
    assert (dest / ".auto-loop" / "reviews").is_dir()
    state = load_lifecycle_state(dest)
    assert state is not None
    assert state.plan_approved is True


def test_help_teaches_canonical_commands_not_internal_files():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    plain = Text.from_ansi(result.stdout).plain
    for name in ("init", "run", "status", "resume"):
        assert name in plain
    run_help = Text.from_ansi(runner.invoke(app, ["run", "--help"]).stdout).plain
    assert "--goal-file" in run_help
    assert "--context" in run_help
    assert ".auto-loop/task.md" not in run_help
    init_help = Text.from_ansi(runner.invoke(app, ["init", "--help"]).stdout).plain
    assert "auto-loop.yaml" in init_help
    assert "task.md" not in init_help.lower() or "does not create a goal" in init_help.lower()
    status_help = Text.from_ansi(runner.invoke(app, ["status", "--help"]).stdout).plain
    resume_help = Text.from_ansi(runner.invoke(app, ["resume", "--help"]).stdout).plain
    assert "tool-managed" in status_help.lower() or ".auto-loop" in status_help
    assert "stored run" in resume_help.lower() or "saved goal" in resume_help.lower()


def test_legacy_root_task_is_used_when_no_goal_flag(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    (repo / "task.md").write_text("Legacy root task body\n", encoding="utf-8")
    prepared = prepare_repo_for_run(repo, RunInputs())
    assert "Legacy root task body" in prepared.goal_text


def test_resume_cli_keeps_stored_goal(tmp_path: Path, monkeypatch):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Original goal"))
    from auto_loop.git import head_commit
    from auto_loop.loop import RunOutcome

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    (repo / "goal.md").write_text("Changed goal\n", encoding="utf-8")
    seen: dict[str, RunInputs] = {}

    def fake_run(_repo, _options, _invoker, *, inputs=None):
        assert inputs is not None
        seen["inputs"] = inputs
        return RunOutcome(exit_code=ExitCode.LIMIT_REACHED)

    monkeypatch.setattr("auto_loop.cli.run_lifecycle", fake_run)
    result = runner.invoke(app, ["resume", str(repo), "--quiet"])
    assert result.exit_code == int(ExitCode.LIMIT_REACHED)
    assert seen["inputs"].resume_only is True
    assert "Original goal" in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")


def test_migrate_preserves_resumable_session(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, goal="Session goal")
    from auto_loop.git import head_commit

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    lifecycle_id = state.lifecycle_id
    (repo / "auto-loop.yaml").unlink()
    migrate = migrate_legacy_layout(repo)
    assert migrate.migrated is True
    resumed = prepare_repo_for_run(repo, RunInputs(resume_only=True))
    assert resumed.is_resume is True
    assert "Session goal" in resumed.goal_text
    assert load_lifecycle_state(repo).lifecycle_id == lifecycle_id


def test_run_without_new_goal_continues_in_progress_run(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Continue me"))
    from auto_loop.git import head_commit

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    prepared = prepare_repo_for_run(repo, RunInputs())
    assert prepared.is_resume is True
    assert "Continue me" in prepared.goal_text


def test_new_goal_after_completed_run_starts_fresh(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="First goal"))
    from auto_loop.git import head_commit
    from auto_loop.terminal_records import completion_path, load_completion_record
    from datetime import datetime, timezone

    from auto_loop.terminal_records import CompletionRecord

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    completion_path(repo).parent.mkdir(parents=True, exist_ok=True)
    record = CompletionRecord(
        completed_at=datetime.now(timezone.utc),
        lifecycle_id="test-lifecycle",
        turn=1,
        worker_session_id="w1",
        reviewer_session_id="r1",
        initial_base_commit=head_commit(repo),
        final_commit=head_commit(repo),
        last_approved_commit=head_commit(repo),
        final_review_file=".auto-loop/reviews/done.md",
        task_sha256="abc",
    )
    completion_path(repo).write_text(record.model_dump_json(), encoding="utf-8")
    assert load_completion_record(repo) is not None

    (repo / ".auto-loop" / "plan.md").write_text("# Old plan from prior run\n", encoding="utf-8")
    (repo / ".auto-loop" / "context.yaml").write_text("version: 1\nshared:\n  resources: [stale]\n", encoding="utf-8")
    (repo / ".auto-loop" / "reviews").mkdir(parents=True, exist_ok=True)
    (repo / ".auto-loop" / "reviews" / "0001-old.md").write_text("# old review\n", encoding="utf-8")

    prepared = prepare_repo_for_run(repo, RunInputs(goal_text="Second goal"))
    assert prepared.is_resume is False
    assert "Second goal" in prepared.goal_text
    assert load_completion_record(repo) is None
    assert load_lifecycle_state(repo) is None
    assert "Old plan from prior run" not in (repo / ".auto-loop" / "plan.md").read_text(encoding="utf-8")
    assert "stale" not in (repo / ".auto-loop" / "context.yaml").read_text(encoding="utf-8")
    assert not list((repo / ".auto-loop" / "reviews").glob("*.md"))
    archived = list((repo / ".auto-loop" / "runtime" / "archives").rglob("0001-old.md"))
    assert archived


def test_new_goal_after_blocked_run_starts_fresh(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Blocked goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import BlockedRecord, blocked_path, load_blocked_record

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    blocked_path(repo).parent.mkdir(parents=True, exist_ok=True)
    blocked_path(repo).write_text(
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            summary="external blocker",
        ).model_dump_json(),
        encoding="utf-8",
    )
    assert load_blocked_record(repo) is not None
    (repo / ".auto-loop" / "reviews").mkdir(parents=True, exist_ok=True)
    (repo / ".auto-loop" / "reviews" / "0099-blocked.md").write_text("# blocked review\n", encoding="utf-8")

    prepared = prepare_repo_for_run(repo, RunInputs(goal_text="Replacement goal"))
    assert prepared.is_resume is False
    assert "Replacement goal" in prepared.goal_text
    assert load_blocked_record(repo) is None
    assert not list((repo / ".auto-loop" / "reviews").glob("*.md"))


def test_new_goal_preserves_custom_agent_templates(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="First goal"))
    marker = "CUSTOM_AGENT_MARKER_XYZ"
    worker = repo / ".auto-loop" / "agents" / "worker.md"
    worker.write_text(f"{marker}\n", encoding="utf-8")
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import CompletionRecord, completion_path, load_completion_record

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    completion_path(repo).write_text(
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id="lc-1",
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            initial_base_commit=head_commit(repo),
            final_commit=head_commit(repo),
            last_approved_commit=head_commit(repo),
            final_review_file=".auto-loop/reviews/done.md",
            task_sha256="abc",
        ).model_dump_json(),
        encoding="utf-8",
    )
    assert load_completion_record(repo) is not None

    prepare_repo_for_run(repo, RunInputs(goal_text="Next goal"))
    assert marker in worker.read_text(encoding="utf-8")


def test_missing_goal_file_leaves_terminal_run_intact(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Done goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import CompletionRecord, completion_path, load_completion_record

    plan = repo / ".auto-loop" / "plan.md"
    plan.write_text("# Old plan marker\n", encoding="utf-8")
    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    completion_path(repo).write_text(
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id="lc-1",
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            initial_base_commit=head_commit(repo),
            final_commit=head_commit(repo),
            last_approved_commit=head_commit(repo),
            final_review_file=".auto-loop/reviews/done.md",
            task_sha256="abc",
        ).model_dump_json(),
        encoding="utf-8",
    )
    with pytest.raises(RunInputError, match="Goal file not found"):
        prepare_repo_for_run(repo, RunInputs(goal_file=Path("does-not-exist.md")))
    assert load_completion_record(repo) is not None
    assert "Old plan marker" in plan.read_text(encoding="utf-8")


def test_blocked_terminal_resume_returns_blocked_exit(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Blocked goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import BlockedRecord, blocked_path

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    blocked_path(repo).write_text(
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            summary="external blocker",
        ).model_dump_json(),
        encoding="utf-8",
    )
    provider = ScriptedProvider()
    outcome = run_lifecycle(repo, run_opts(2), provider, inputs=RunInputs(resume_only=True))
    assert outcome.exit_code == ExitCode.BLOCKED
    assert outcome.message == "external blocker"


def test_blocked_terminal_run_without_new_goal_returns_blocked(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Blocked goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import BlockedRecord, blocked_path

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    blocked_path(repo).write_text(
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            summary="still blocked",
        ).model_dump_json(),
        encoding="utf-8",
    )
    provider = ScriptedProvider()
    outcome = run_lifecycle(repo, run_opts(2), provider, inputs=RunInputs())
    assert outcome.exit_code == ExitCode.BLOCKED
    assert outcome.message == "still blocked"


def _repo_file_snapshot(repo: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(repo): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file()
    }


def _seed_completed_terminal_run(repo: Path) -> None:
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import CompletionRecord, completion_path, load_completion_record

    save_lifecycle_state(repo, create_lifecycle(head_commit(repo)))
    completion_path(repo).parent.mkdir(parents=True, exist_ok=True)
    completion_path(repo).write_text(
        CompletionRecord(
            completed_at=datetime.now(timezone.utc),
            lifecycle_id="lc-terminal",
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            initial_base_commit=head_commit(repo),
            final_commit=head_commit(repo),
            last_approved_commit=head_commit(repo),
            final_review_file=".auto-loop/reviews/done.md",
            task_sha256="abc",
        ).model_dump_json(),
        encoding="utf-8",
    )
    assert load_completion_record(repo) is not None


def test_empty_goal_file_preserves_terminal_run(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Done goal"))
    _seed_completed_terminal_run(repo)
    (repo / "empty-goal.md").write_text("\n\n", encoding="utf-8")
    before = _repo_file_snapshot(repo)
    with pytest.raises(RunInputError, match="empty"):
        prepare_repo_for_run(repo, RunInputs(goal_file=Path("empty-goal.md")))
    assert _repo_file_snapshot(repo) == before


def test_invalid_context_preserves_terminal_run(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Done goal"))
    _seed_completed_terminal_run(repo)
    (repo / "bad-context.yaml").write_text("version: 99\n", encoding="utf-8")
    before = _repo_file_snapshot(repo)
    with pytest.raises(RunInputError):
        prepare_repo_for_run(
            repo,
            RunInputs(goal_text="Next goal", context_file=Path("bad-context.yaml")),
        )
    assert _repo_file_snapshot(repo) == before


def test_archive_uses_frozen_config_not_current_user_yaml(tmp_path: Path):
    from auto_loop.config import load_config_from_repo, write_resolved_config

    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="First goal"))
    frozen = load_config_from_repo(repo)
    frozen.plan_file = ".auto-loop/design.md"
    frozen.reviews_dir = ".auto-loop/run-reviews"
    frozen.logging.event_log = ".auto-loop/runtime/custom-events.jsonl"
    write_resolved_config(repo, frozen)
    design = repo / ".auto-loop" / "design.md"
    design.write_text("# frozen plan marker\n", encoding="utf-8")
    review_dir = repo / ".auto-loop" / "run-reviews"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "0001-old.md").write_text("# old review\n", encoding="utf-8")
    events = repo / ".auto-loop" / "runtime" / "custom-events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("event line\n", encoding="utf-8")
    _seed_completed_terminal_run(repo)

    prepare_repo_for_run(repo, RunInputs(goal_text="Second goal"))
    assert not design.is_file()
    archive_root = repo / ".auto-loop" / "runtime" / "archives"
    run_archive = next(archive_root.iterdir())
    archived_plan = run_archive / ".auto-loop" / "design.md"
    assert archived_plan.is_file()
    assert "frozen plan marker" in archived_plan.read_text(encoding="utf-8")
    old_review = run_archive / ".auto-loop" / "run-reviews" / "0001-old.md"
    assert old_review.is_file()
    assert (run_archive / ".auto-loop" / "runtime" / "custom-events.jsonl").is_file()
    assert (repo / ".auto-loop" / "plan.md").is_file()


def test_blocked_resume_before_invalid_context_prerequisites(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Blocked goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import BlockedRecord, blocked_path

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    blocked_path(repo).write_text(
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            summary="blocked despite bad context",
        ).model_dump_json(),
        encoding="utf-8",
    )
    (repo / ".auto-loop" / "context.yaml").write_text("version: 99\n", encoding="utf-8")
    provider = ScriptedProvider()
    outcome = run_lifecycle(repo, run_opts(2), provider, inputs=RunInputs(resume_only=True))
    assert outcome.exit_code == ExitCode.BLOCKED
    assert outcome.message == "blocked despite bad context"


def test_run_lifecycle_new_goal_replaces_blocked_without_prepare_first(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Blocked goal"))
    from datetime import datetime, timezone

    from auto_loop.git import head_commit
    from auto_loop.terminal_records import BlockedRecord, blocked_path, load_blocked_record

    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    blocked_path(repo).write_text(
        BlockedRecord(
            blocked_at=datetime.now(timezone.utc),
            lifecycle_id=state.lifecycle_id,
            turn=1,
            worker_session_id="w1",
            reviewer_session_id="r1",
            summary="external blocker",
        ).model_dump_json(),
        encoding="utf-8",
    )
    assert load_blocked_record(repo) is not None
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(
        repo,
        run_opts(2),
        provider,
        inputs=RunInputs(goal_text="Replacement goal"),
    )
    assert load_blocked_record(repo) is None
    assert "Replacement goal" in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")
    assert not (outcome.exit_code == ExitCode.BLOCKED and outcome.message == "external blocker")


def test_archive_includes_prior_task_snapshot(tmp_path: Path):
    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="First archived goal"))
    _seed_completed_terminal_run(repo)
    before_task = (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")
    assert "First archived goal" in before_task

    prepare_repo_for_run(repo, RunInputs(goal_text="Second goal"))
    archive_root = repo / ".auto-loop" / "runtime" / "archives"
    run_archive = next(archive_root.iterdir())
    archived_task = run_archive / ".auto-loop" / "task.md"
    assert archived_task.is_file()
    assert "First archived goal" in archived_task.read_text(encoding="utf-8")
    assert "Second goal" in (repo / ".auto-loop" / "task.md").read_text(encoding="utf-8")


def test_archive_preserves_relative_paths_for_same_basename(tmp_path: Path):
    from auto_loop.config import load_config_from_repo, write_resolved_config

    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Goal with split paths"))
    frozen = load_config_from_repo(repo)
    frozen.task_file = ".auto-loop/store/a/task.md"
    frozen.context_file = ".auto-loop/store/b/task.md"
    write_resolved_config(repo, frozen)
    task_a = repo / frozen.task_file
    task_b = repo / frozen.context_file
    task_a.parent.mkdir(parents=True, exist_ok=True)
    task_b.parent.mkdir(parents=True, exist_ok=True)
    task_a.write_text("actual goal body\n", encoding="utf-8")
    task_b.write_text("context body\n", encoding="utf-8")
    _seed_completed_terminal_run(repo)

    prepare_repo_for_run(repo, RunInputs(goal_text="Next"))
    archive_root = repo / ".auto-loop" / "runtime" / "archives"
    run_archive = next(archive_root.iterdir())
    archived_goal = run_archive / ".auto-loop" / "store" / "a" / "task.md"
    archived_context = run_archive / ".auto-loop" / "store" / "b" / "task.md"
    assert archived_goal.is_file()
    assert archived_context.is_file()
    assert "actual goal body" in archived_goal.read_text(encoding="utf-8")
    assert "context body" in archived_context.read_text(encoding="utf-8")


def test_archive_rejects_paths_escaping_workspace(tmp_path: Path):
    from auto_loop.config import load_config_from_repo, write_resolved_config
    from auto_loop.terminal_records import load_completion_record

    repo = git_repo(tmp_path)
    run_init(repo)
    prepare_repo_for_run(repo, RunInputs(goal_text="Prior goal"))
    frozen = load_config_from_repo(repo)
    frozen.task_file = "../../../outside-goal.md"
    write_resolved_config(repo, frozen)
    escape_target = (repo / frozen.task_file).resolve()
    escape_target.parent.mkdir(parents=True, exist_ok=True)
    escape_target.write_text("outside secret\n", encoding="utf-8")
    _seed_completed_terminal_run(repo)
    before = _repo_file_snapshot(repo)
    with pytest.raises(RunInputError, match="escapes workspace"):
        prepare_repo_for_run(repo, RunInputs(goal_text="Next goal"))
    assert _repo_file_snapshot(repo) == before
    assert load_completion_record(repo) is not None
