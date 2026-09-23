"""Blocked lifecycle suspension and resume."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_loop.blocked_resume import (
    finalize_unblocked_archive,
    persist_unblock_state,
    reconcile_blocked_resume,
    resolve_resume_session,
)
from auto_loop.config import load_resolved_config_optional
from auto_loop.exits import ExitCode
from auto_loop.lifecycle import LifecycleStatus, create_lifecycle, utc_now
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_inputs import (
    has_active_lifecycle,
    has_suspended_blocked_record,
    has_terminal_record,
    prepare_repo_for_run,
    RunInputError,
)
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import (
    BlockedRecord,
    blocked_path,
    load_blocked_record,
    save_blocked_record,
)
from auto_loop.events import load_events
from tests.integration.scenario_harness import approve_plan, make_repo, run_lifecycle, run_opts


def test_blocked_is_active_not_terminal(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = create_lifecycle(None)
    state.status = LifecycleStatus.BLOCKED
    save_lifecycle_state(repo, state)
    save_blocked_record(
        repo,
        BlockedRecord(
            blocked_at=utc_now(),
            lifecycle_id=state.lifecycle_id,
            turn=state.turn,
            worker_session_id="w",
            reviewer_session_id="r",
            summary="waiting on CI",
        ),
    )
    assert has_suspended_blocked_record(repo)
    assert has_active_lifecycle(repo)
    assert not has_terminal_record(repo)


def test_run_refuses_while_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked()
    run_lifecycle(repo, run_opts(3), provider)
    source = load_run_manifest(repo / ".ai" / "run.yaml")
    with pytest.raises(RunInputError, match="already in progress"):
        prepare_repo_for_run(source, resume=False)


def test_resume_consumes_blocked_record_and_runs_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked("hosted-workflow-missing")
    blocked_outcome = run_lifecycle(repo, run_opts(3), provider)
    assert blocked_outcome.exit_code == ExitCode.BLOCKED
    assert blocked_path(repo).is_file()
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.BLOCKED
    assert state.next_session == "worker"

    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    resumed = run_lifecycle(repo, run_opts(3, resuming=True), provider)
    assert resumed.exit_code == ExitCode.COMPLETE
    assert load_blocked_record(repo) is None
    assert not blocked_path(repo).is_file()
    archive = list((repo / ".ai/auto-loop/runtime/archive").glob("blocked-*.json"))
    assert archive
    events = load_events(repo, load_run_manifest(repo / ".ai/run.yaml").config)
    assert any(e.get("type") == "lifecycle_resumed_from_blocked" for e in events)


def test_idempotent_run_still_blocked_without_resume_flag(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked()
    run_lifecycle(repo, run_opts(3), provider)
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.BLOCKED


def test_legacy_blocked_record_infers_resume_session():
    state = create_lifecycle(None)
    state.phase = "execution"
    record = BlockedRecord(
        blocked_at=utc_now(),
        lifecycle_id=state.lifecycle_id,
        turn=1,
        worker_session_id="w",
        reviewer_session_id="r",
        summary="legacy",
    )
    assert resolve_resume_session(record, state) == "worker"
    state.phase = "planning"
    assert resolve_resume_session(record, state) == "planner"


def test_unblock_persist_before_archive_survives_crash(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked("CI gate")
    run_lifecycle(repo, run_opts(3), provider)
    state = load_lifecycle_state(repo)
    record = load_blocked_record(repo)
    assert state is not None and record is not None
    assert state.status == LifecycleStatus.BLOCKED
    assert blocked_path(repo).is_file()

    source = load_run_manifest(repo / ".ai/run.yaml")
    config = load_resolved_config_optional(source.artifact_root) or source.config
    state = persist_unblock_state(state, record)
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    assert state.status == LifecycleStatus.RUNNING
    assert state.blocked_resume_context is not None
    assert blocked_path(repo).is_file()

    state = reconcile_blocked_resume(
        repo, config, load_lifecycle_state(repo, source.artifact_root), artifact_root=source.artifact_root
    )
    assert load_blocked_record(repo) is None
    archives = list((repo / ".ai/auto-loop/runtime/archive").glob("blocked-*-turn*.json"))
    assert len(archives) == 1
    assert "turn" in archives[0].name
    events = [e for e in load_events(repo, config) if e.get("type") == "lifecycle_resumed_from_blocked"]
    assert len(events) == 1

    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(3, resuming=True), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    events = [e for e in load_events(repo, config) if e.get("type") == "lifecycle_resumed_from_blocked"]
    assert len(events) == 1


def test_finalize_unblock_is_idempotent(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = create_lifecycle(None)
    state.status = LifecycleStatus.BLOCKED
    save_lifecycle_state(repo, state)
    record = BlockedRecord(
        blocked_at=utc_now(),
        lifecycle_id=state.lifecycle_id,
        turn=state.turn,
        worker_session_id="w",
        reviewer_session_id="r",
        summary="blocked",
        resume_session="worker",
        blocked_by_session="reviewer",
    )
    save_blocked_record(repo, record)
    source = load_run_manifest(repo / ".ai/run.yaml")
    config = load_resolved_config_optional(source.artifact_root) or source.config

    state = persist_unblock_state(state, record)
    save_lifecycle_state(repo, state, artifact_root=source.artifact_root)
    state = finalize_unblocked_archive(repo, config, state, record, artifact_root=source.artifact_root)
    assert not blocked_path(repo).is_file()
    state = finalize_unblocked_archive(repo, config, state, record, artifact_root=source.artifact_root)
    events = [e for e in load_events(repo, config) if e.get("type") == "lifecycle_resumed_from_blocked"]
    assert len(events) == 1
