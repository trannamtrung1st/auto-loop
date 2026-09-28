"""Blocked-worker control flow and repeat-guard regression tests."""

from __future__ import annotations

import json
from pathlib import Path

from auto_loop.blockers import compute_blocker_fingerprint, normalize_blocker_summary
from auto_loop.exits import ExitCode
from auto_loop.lifecycle import LifecycleStatus
from auto_loop.runtime import load_lifecycle_state
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.terminal_records import load_blocked_record
from tests.integration.scenario_harness import approve_plan, make_repo, run_lifecycle, run_opts


def test_normalize_blocker_summary_collapses_whitespace():
    assert normalize_blocker_summary("  hosted CI\nnot green  ") == "hosted ci not green"


def test_blocker_fingerprint_includes_head():
    a = compute_blocker_fingerprint(summary="need push", head="abc")
    b = compute_blocker_fingerprint(summary="need push", head="def")
    assert a != b


def test_worker_blocked_reviewer_pass_terminates_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked("hosted synthetic CI has not run for HEAD")
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.BLOCKED
    assert state.next_session == "worker"
    record = load_blocked_record(repo)
    assert record is not None
    assert record.blocked_by_session == "reviewer"
    assert record.blocker_fingerprint is not None


def test_repeated_identical_worker_blocker_skips_extra_loop(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    summary = "hosted synthetic CI has not run for HEAD"
    provider.set_worker_blocked(summary)
    provider.set_reviewer_revise("batch", "blocked")
    provider.set_worker_blocked(summary)
    outcome = run_lifecycle(repo, run_opts(6), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    record = load_blocked_record(repo)
    assert record is not None
    assert record.blocked_by_session == "worker"
    blocked = json.loads((repo / ".ai/auto-loop/runtime/blocked.json").read_text(encoding="utf-8"))
    assert blocked["summary"] == summary


def test_worker_blocked_without_reviewer_review_terminates(tmp_path: Path):
    from auto_loop.config import load_resolved_config_optional
    from auto_loop.manifest import load_run_manifest

    repo = make_repo(tmp_path)
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = load_resolved_config_optional(manifest.artifact_root) or manifest.config
    config.run.require_blocker_review = False
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked("credential missing in hosted environment")
    outcome = run_lifecycle(repo, run_opts(2), provider, config=config)
    assert outcome.exit_code == ExitCode.BLOCKED
    record = load_blocked_record(repo)
    assert record is not None
    assert record.blocked_by_session == "worker"
