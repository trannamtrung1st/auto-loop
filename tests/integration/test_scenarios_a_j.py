"""Proposal section 44 workflow scenarios A-J (deterministic fake provider)."""

from __future__ import annotations

import subprocess
from pathlib import Path


from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state
from auto_loop.terminal_records import load_completion_record
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    approve_plan,
    batch_worker_payload,
    commit_file,
    latest_review_text,
    make_repo,
    reviewer_invocation_count,
    run_opts,
)


def test_scenario_A_happy_path(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "feature B")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    mid = load_lifecycle_state(repo)
    worker_id = mid.sessions["worker"].session_id
    reviewer_id = mid.sessions["reviewer"].session_id
    assert worker_id and reviewer_id and worker_id != reviewer_id
    provider.set_worker_final_request(head=mid.last_approved_commit)
    provider.set_reviewer_complete(mid.last_approved_commit)
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    record = load_completion_record(repo)
    assert record is not None
    assert record.final_commit == head_b
    final = load_lifecycle_state(repo)
    assert final.sessions["worker"].session_id == worker_id
    assert final.sessions["reviewer"].session_id == reviewer_id


def test_scenario_B_plan_revision_no_product_commits_before_pass(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    initial = head_commit(repo)
    provider.set_worker_plan_request()
    provider.set_reviewer_revise("plan", "plan")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(4), provider)
    state = load_lifecycle_state(repo)
    assert state.plan_approved is True
    count = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{initial}..HEAD"],
            cwd=repo,
            text=True,
        ).strip()
        or "0"
    )
    assert count == 0
    assert head_commit(repo) == initial


def test_scenario_C_batch_revision_reviews_cumulative_A_to_C_not_B_to_C(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "commit B")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    head_c = commit_file(repo, "c.txt", "c\n", "commit C")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    prompt = provider.reviewer_prompts[-1]
    assert f"{baseline}..{head_c}" in prompt
    assert f"{head_b}..{head_c}" not in prompt
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == head_c


def test_scenario_D_multiple_revision_commits_accept_A_to_D(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "B")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    head_c = commit_file(repo, "c.txt", "c\n", "C")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    head_d = commit_file(repo, "d.txt", "d\n", "D")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    prompt = provider.reviewer_prompts[-1]
    assert f"{baseline}..{head_d}" in prompt
    assert load_lifecycle_state(repo).last_approved_commit == head_d


def test_scenario_E_wrong_base_normalized_to_approved_through_head(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "B")
    head_c = commit_file(repo, "c.txt", "c\n", "C")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    prompt = provider.reviewer_prompts[-1]
    assert f"{baseline}..{head_c}" in prompt
    assert f"{head_b}..{head_c}" not in prompt
    assert load_lifecycle_state(repo).last_approved_commit == head_c


def test_scenario_F_dirty_batch_suppresses_reviewer_until_exhaustion(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(2), provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    provider.set_response("worker", batch_worker_payload())
    provider.set_response("worker", batch_worker_payload())
    reviewers_before_batch = reviewer_invocation_count(provider)
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    assert reviewer_invocation_count(provider) == reviewers_before_batch
    assert load_lifecycle_state(repo).last_approved_commit == baseline


def test_scenario_G_history_rewrite_stops_with_git_protocol_error(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    commit_file(repo, "feature.txt", "x\n", "feature")
    head = head_commit(repo)
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    subprocess.run(["git", "reset", "--hard", baseline], cwd=repo, check=True)
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


def test_scenario_H_final_rejected_when_head_ahead_of_approved(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    commit_file(repo, "feature.txt", "x\n", "feature")
    head = head_commit(repo)
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    commit_file(repo, "extra.txt", "e\n", "extra")
    provider.set_worker_final_request()
    outcome = run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    assert state.next_actor == "worker"
    assert state.active_review is None
    assert head_commit(repo) != approved
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_scenario_I_final_revise_routes_through_batch_before_complete(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    commit_file(repo, "feature.txt", "x\n", "feature")
    head_c = head_commit(repo)
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    approved_c = load_lifecycle_state(repo).last_approved_commit
    provider.set_worker_final_request(head=approved_c)
    provider.set_reviewer_revise("final", "whole-task")
    run_lifecycle(repo, run_opts(2), provider)
    head_d = commit_file(repo, "fix.txt", "fix\n", "fix D")
    provider.set_response("worker", batch_worker_payload("W02"))
    provider.set_reviewer_pass("batch", "W02")
    provider.set_worker_final_request(head=head_d)
    provider.set_reviewer_complete(head_d)
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    assert load_completion_record(repo).final_commit == head_d


def test_scenario_J_reviewer_finding_outside_diff_is_accepted(tmp_path: Path):
    repo = make_repo(tmp_path)
    commit_file(repo, "outside.txt", "related context\n", "outside context")
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "B")
    provider.set_response("worker", batch_worker_payload())
    provider.set_reviewer_revise(
        "batch",
        "W01",
        finding_id="J-1",
    )
    run_lifecycle(repo, run_opts(2), provider)
    text = latest_review_text(repo)
    assert "REVISE" in text.upper() or "revise" in text.lower()
    assert "J-1" in text or "Fix required" in text
    assert load_lifecycle_state(repo).last_approved_commit == baseline
