"""Approved-baseline remap after an intentional rewrite of reviewed history."""

from __future__ import annotations

import subprocess
from pathlib import Path

from auto_loop.config import load_resolved_config_optional
from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.history_reconciliation import reconcile_approved_history
from auto_loop.lifecycle import HistoryReconciliation
from auto_loop.manifest import load_run_manifest
from auto_loop.paths import resolved_artifact_root
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.status_report import build_status_report
from tests.integration.scenario_harness import (
    approve_plan,
    batch_worker_payload,
    commit_file,
    git,
    make_repo,
    run_lifecycle,
    run_opts,
    worker_invocation_count,
)


def _rev(repo: Path, ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", ref], cwd=repo, text=True).strip()


def _commit_tree(repo: Path, tree: str, parent: str, message: str) -> str:
    return subprocess.check_output(
        ["git", "commit-tree", tree, "-p", parent, "-m", message],
        cwd=repo,
        text=True,
    ).strip()


def _frozen_config(repo: Path):
    source = load_run_manifest(repo / ".ai" / "run.yaml")
    root = resolved_artifact_root(repo, source.config.artifacts_root)
    config = load_resolved_config_optional(root) or source.config
    return config, root


def _reconciliation_events(repo: Path) -> list[dict]:
    config, _root = _frozen_config(repo)
    return [event for event in load_events(repo, config) if event.get("type") == "history_reconciliation"]


def _approve_feature_then_commit_more(repo: Path, provider: ScriptedProvider) -> tuple[str, str]:
    approve_plan(repo, provider)
    approved = commit_file(repo, "feature.txt", "approved\n", "approved")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == approved
    head = commit_file(repo, "extra.txt", "later\n", "later unreviewed")
    return approved, head


def _rewrite_history(
    repo: Path,
    approved: str,
    head: str,
    *,
    ambiguous: bool = False,
) -> tuple[str, str, str]:
    """Replay approved and HEAD with new SHAs and the same trees.

    Returns ``(equivalent, extra_equivalent_or_empty, new_head)``.
    ``extra_equivalent_or_empty`` is set only when a second commit shares the
    approved product tree.
    """
    parent = _rev(repo, f"{approved}^")
    tree_approved = _rev(repo, f"{approved}^{{tree}}")
    tree_head = _rev(repo, f"{head}^{{tree}}")
    candidate = _commit_tree(repo, tree_approved, parent, "rebased approved")
    extra = ""
    tip_parent = candidate
    if ambiguous:
        extra = _commit_tree(repo, tree_approved, candidate, "rebased approved duplicate")
        tip_parent = extra
    new_head = _commit_tree(repo, tree_head, tip_parent, "rebased later")
    git(repo, "reset", "--hard", new_head)
    return candidate, extra, new_head


def test_midpoint_rebase_remaps_equivalent_commit_and_leaves_later_work_unapproved(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved, original_head = _approve_feature_then_commit_more(repo, provider)
    candidate, _extra, new_head = _rewrite_history(repo, approved, original_head)
    assert candidate != new_head
    assert candidate != approved

    provider.set_response("worker", batch_worker_payload())
    outcome = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert outcome.exit_code != ExitCode.GIT_PROTOCOL_ERROR
    assert outcome.exit_code != ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == candidate
    assert state.last_approved_commit != new_head
    assert head_commit(repo) == new_head
    assert state.history_reconciliation is not None
    assert state.history_reconciliation.applied is True
    git_evidence = [item for item in state.approved_evidence if item.kind == "git_range"]
    assert git_evidence
    assert all(item.git_head != approved for item in git_evidence)
    assert any(item.git_head == candidate for item in git_evidence)
    assert any(item.fingerprint == candidate for item in git_evidence)

    events = _reconciliation_events(repo)
    assert len(events) == 1
    assert events[0]["old_sha"] == approved
    assert events[0]["new_sha"] == candidate
    assert events[0]["head"] == new_head
    assert "product_tree_fingerprint" in events[0]["evidence"]
    assert "ancestor_of_head" in events[0]["evidence"]

    provider.set_reviewer_pass("batch", "W01")
    reviewed = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert reviewed.exit_code != ExitCode.GIT_PROTOCOL_ERROR
    assert reviewed.exit_code != ExitCode.COMPLETE
    after_review = load_lifecycle_state(repo)
    assert after_review is not None
    assert after_review.last_approved_commit == new_head


def test_ambiguous_rebase_mapping_refuses_to_move_the_approved_baseline(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved, original_head = _approve_feature_then_commit_more(repo, provider)
    _candidate, extra, new_head = _rewrite_history(
        repo, approved, original_head, ambiguous=True
    )
    workers_before = worker_invocation_count(provider)
    outcome = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    assert worker_invocation_count(provider) == workers_before
    assert outcome.message is not None
    assert "Git protocol error" in outcome.message
    assert "Invariant:" in outcome.message
    assert "ambiguous" in outcome.message
    assert "State: preserved" in outcome.message
    assert "not moved to HEAD" in outcome.message
    assert approved in outcome.message
    assert new_head in outcome.message
    assert extra in outcome.message

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.last_approved_commit == approved
    assert state.history_reconciliation is not None
    assert state.history_reconciliation.needs_decision is True
    assert state.history_reconciliation.applied is False
    assert state.history_reconciliation.new_sha is None
    assert extra in state.history_reconciliation.candidates
    assert _reconciliation_events(repo) == []

    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "NEEDS HISTORY RECONCILIATION" in report
    assert "approved baseline: unchanged" in report

    again = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert again.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    reloaded = load_lifecycle_state(repo)
    assert reloaded is not None
    assert reloaded.last_approved_commit == approved


def test_restart_during_reconciliation_finishes_one_mapping(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved, original_head = _approve_feature_then_commit_more(repo, provider)
    candidate, _extra, new_head = _rewrite_history(repo, approved, original_head)
    state = load_lifecycle_state(repo)
    assert state is not None
    state.history_reconciliation = HistoryReconciliation(
        old_sha=approved,
        new_sha=candidate,
        head_sha=new_head,
        evidence=["product_tree_fingerprint", "ancestor_of_head"],
        candidates=[candidate],
        applied=False,
        needs_decision=False,
    )
    save_lifecycle_state(repo, state)

    provider.set_response("worker", batch_worker_payload())
    outcome = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert outcome.exit_code != ExitCode.GIT_PROTOCOL_ERROR
    resumed = load_lifecycle_state(repo)
    assert resumed is not None
    assert resumed.last_approved_commit == candidate
    assert resumed.last_approved_commit != new_head
    assert resumed.history_reconciliation is not None
    assert resumed.history_reconciliation.applied is True
    assert len(_reconciliation_events(repo)) == 1

    config, root = _frozen_config(repo)
    second = reconcile_approved_history(
        repo,
        config,
        resumed,
        artifact_root=root,
    )
    assert second.last_approved_commit == candidate
    assert len(_reconciliation_events(repo)) == 1


def test_pending_mapping_to_head_is_discarded_when_a_midpoint_equivalent_exists(
    tmp_path: Path,
):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approved, original_head = _approve_feature_then_commit_more(repo, provider)
    candidate, _extra, new_head = _rewrite_history(repo, approved, original_head)
    state = load_lifecycle_state(repo)
    assert state is not None
    state.history_reconciliation = HistoryReconciliation(
        old_sha=approved,
        new_sha=new_head,
        head_sha=new_head,
        evidence=["product_tree_fingerprint"],
        candidates=[new_head],
        applied=False,
        needs_decision=False,
    )
    save_lifecycle_state(repo, state)

    provider.set_response("worker", batch_worker_payload())
    outcome = run_lifecycle(repo, run_opts(1, resuming=True), provider)
    assert outcome.exit_code != ExitCode.GIT_PROTOCOL_ERROR
    resumed = load_lifecycle_state(repo)
    assert resumed is not None
    assert resumed.last_approved_commit == candidate
    assert resumed.last_approved_commit != new_head
    events = _reconciliation_events(repo)
    assert len(events) == 1
    assert events[0]["new_sha"] == candidate
    assert events[0]["new_sha"] != new_head
