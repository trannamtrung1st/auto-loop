"""Blocked-worker control flow and repeat-guard regression tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from auto_loop.blockers import (
    BlockerFingerprintInput,
    compute_blocker_fingerprint,
    normalize_blocker_summary,
    product_evidence_fingerprint,
)
from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import LifecycleStatus
from auto_loop.manifest import load_run_manifest
from auto_loop.runtime import load_lifecycle_state
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.terminal_records import load_blocked_record
from tests.integration.scenario_harness import (
    approve_plan,
    execution_reviewer_invocation_count,
    git,
    make_repo,
    run_lifecycle,
    run_opts,
)


def test_normalize_blocker_summary_collapses_whitespace():
    assert normalize_blocker_summary("  hosted CI\nnot green  ") == "hosted ci not green"


def test_blocker_fingerprint_includes_git_head():
    base = BlockerFingerprintInput(
        phase="execution",
        implementer_slot="worker",
        summary="need push",
        git_head="abc",
        plan_sha256=None,
        evidence_fingerprint="ev1",
    )
    other_head = replace(base, git_head="def")
    assert compute_blocker_fingerprint(base) != compute_blocker_fingerprint(other_head)


def test_blocker_fingerprint_includes_evidence():
    base = BlockerFingerprintInput(
        phase="execution",
        implementer_slot="worker",
        summary="external gate",
        git_head=None,
        plan_sha256=None,
        evidence_fingerprint="ev1",
    )
    other_evidence = replace(base, evidence_fingerprint="ev2")
    assert compute_blocker_fingerprint(base) != compute_blocker_fingerprint(other_evidence)


def test_blocker_fingerprint_includes_plan_hash_for_planner():
    base = BlockerFingerprintInput(
        phase="planning",
        implementer_slot="planner",
        summary="need approval",
        git_head="abc",
        plan_sha256="plan-a",
        evidence_fingerprint="ev1",
    )
    other_plan = replace(base, plan_sha256="plan-b")
    assert compute_blocker_fingerprint(base) != compute_blocker_fingerprint(other_plan)


def test_blocker_fingerprint_includes_plan_hash_for_worker():
    base = BlockerFingerprintInput(
        phase="execution",
        implementer_slot="worker",
        summary="external gate",
        git_head="abc",
        plan_sha256="plan-a",
        evidence_fingerprint="ev1",
    )
    other_plan = replace(base, plan_sha256="plan-b")
    assert compute_blocker_fingerprint(base) != compute_blocker_fingerprint(other_plan)


def test_product_evidence_fingerprint_changes_with_rows():
    a = product_evidence_fingerprint([["src/a.txt", "fs", "deadbeef"]])
    b = product_evidence_fingerprint([["src/a.txt", "fs", "cafebabe"]])
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
    config = load_run_manifest(repo / ".ai/run.yaml").config
    events = load_events(repo, config)
    assert any(
        e.get("type") == "review_result"
        and e.get("verdict") == "pass"
        and e.get("scope") == "batch"
        for e in events
    )


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


def test_same_blocker_after_uncommitted_evidence_change_still_gets_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    summary = "hosted synthetic CI has not run for HEAD"
    head_at_start = head_commit(repo)
    provider.set_worker_blocked(summary)
    provider.set_reviewer_revise("batch", "blocked")
    run_lifecycle(repo, run_opts(2), provider)
    assert load_lifecycle_state(repo).next_session == "worker"
    assert head_commit(repo) == head_at_start
    (repo / "draft-work.txt").write_text("uncommitted product evidence\n", encoding="utf-8")
    provider.set_worker_blocked(summary)
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    assert head_commit(repo) == head_at_start
    assert execution_reviewer_invocation_count(provider) == 2


def test_same_blocker_after_evidence_change_git_mode_off(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    bootstrap_workspace(repo, git_mode="off")
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    run_lifecycle(repo, run_opts(2), provider)
    summary = "vendor credential missing"
    provider.set_worker_blocked(summary)
    provider.set_reviewer_revise("batch", "blocked")
    run_lifecycle(repo, run_opts(2), provider)
    assert load_lifecycle_state(repo).next_session == "worker"
    (repo / "output.txt").write_text("filesystem evidence\n", encoding="utf-8")
    provider.set_worker_blocked(summary)
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    assert execution_reviewer_invocation_count(provider) == 2


def test_worker_plan_update_before_repeat_blocker_gets_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    summary = "hosted synthetic CI has not run for HEAD"
    head_at_start = head_commit(repo)
    provider.set_worker_blocked(summary)
    provider.set_reviewer_revise("batch", "blocked")
    run_lifecycle(repo, run_opts(2), provider)
    plan_path = repo / ".ai/auto-loop/plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n## Worker plan tweak\n", encoding="utf-8")
    provider.set_worker_blocked(summary)
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    assert head_commit(repo) == head_at_start
    assert execution_reviewer_invocation_count(provider) == 2


def test_ignored_product_file_change_does_not_reset_blocker_repeat_guard(tmp_path: Path):
    """Git-ignored product edits are invisible to blocker evidence (documented)."""
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("vendor-out/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore vendor output")
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    summary = "external CI gate"
    provider.set_worker_blocked(summary)
    provider.set_reviewer_revise("batch", "blocked")
    run_lifecycle(repo, run_opts(2), provider)
    (repo / "vendor-out").mkdir(exist_ok=True)
    (repo / "vendor-out" / "result.txt").write_text("ignored artifact\n", encoding="utf-8")
    provider.set_worker_blocked(summary)
    provider.set_reviewer_pass("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    assert execution_reviewer_invocation_count(provider) == 1


def test_same_planner_blocker_after_plan_change_still_gets_reviewer(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    summary = "external planning approval required"
    provider.set_planner_blocked(summary)
    provider.set_reviewer_revise("plan", "blocked", slot="plan_reviewer")
    run_lifecycle(repo, run_opts(2), provider)
    assert load_lifecycle_state(repo).next_session == "planner"
    plan_path = repo / ".ai/auto-loop/plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n## Updated scope\n", encoding="utf-8")
    provider.set_planner_blocked(summary)
    provider.set_reviewer_pass("plan", "blocked", slot="plan_reviewer")
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    plan_reviewer_turns = sum(
        1 for inv in provider.engine.invocations if inv.role == "plan_reviewer"
    )
    assert plan_reviewer_turns == 2


def test_worker_blocked_without_reviewer_review_terminates(tmp_path: Path):
    from auto_loop.config import load_resolved_config_optional

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


def test_require_blocker_review_false_from_run_yaml(tmp_path: Path):
    repo = make_repo(tmp_path)
    run_yaml = repo / ".ai/run.yaml"
    text = run_yaml.read_text(encoding="utf-8")
    if "require_blocker_review" not in text:
        run_yaml.write_text(
            text.replace(
                "max_runtime_minutes: 480",
                "max_runtime_minutes: 480\n  require_blocker_review: false",
            ),
            encoding="utf-8",
        )
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked("waiting on vendor")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    assert execution_reviewer_invocation_count(provider) == 0
