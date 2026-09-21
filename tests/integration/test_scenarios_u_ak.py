"""Proposal section 32 integration scenarios U–AK."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.repo_utils import frozen_config
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.lifecycle import InflightMarker, utc_now
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import build_run_options
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    approve_plan,
    batch_worker_payload,
    commit_file,
    git,
    make_repo,
    run_opts,
)


def _plan_update_payload(target: str = "plan-update") -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {"scope": "plan", "target": target, "summary": "updated plan"},
        "work_summary": "plan update",
        "verification": [],
        "notes": [],
    }


def _path_batch_payload(
    path: str,
    *,
    target_id: str = "generated",
    base: str | None = None,
    head: str | None = None,
    extra_targets: list[dict] | None = None,
) -> dict:
    targets = [
        {
            "kind": "path",
            "id": target_id,
            "path": path,
            "purpose": "intentionally gitignored",
        }
    ]
    if extra_targets:
        targets.extend(extra_targets)
    review: dict = {
        "scope": "batch",
        "target": "generated-validation",
        "summary": "review generated report",
        "targets": targets,
    }
    if base:
        review["base_commit"] = base
    if head:
        review["head_commit"] = head
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": review,
        "work_summary": "generated report",
        "verification": [],
        "notes": [],
    }


def _reviewer_pass_targets(scope: str, target: str, ids: list[str]) -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": scope,
        "target": target,
        "reviewed_target_ids": ids,
        "summary": "ok",
        "findings": [],
        "verification": [],
    }


class WorkerUpdatesPlanProvider(PromptCapturingProvider):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "worker":
            plan = self.repo / ".ai/auto-loop" / "plan.md"
            plan.write_text(plan.read_text(encoding="utf-8") + "\nworker plan update\n", encoding="utf-8")
        return super().invoke(argv)


class ReviewerMutatesPlanProvider(ScriptedProvider):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "reviewer":
            plan = self.repo / ".ai/auto-loop" / "plan.md"
            plan.write_text(plan.read_text(encoding="utf-8") + "\nreviewer plan edit\n", encoding="utf-8")
        return super().invoke(argv)


class ReviewerMutatesPathProvider(ScriptedProvider):
    def __init__(self, repo: Path, rel: str) -> None:
        super().__init__()
        self.repo = repo
        self.rel = rel

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "reviewer":
            path = self.repo / self.rel
            path.write_text("mutated-by-reviewer\n", encoding="utf-8")
        return super().invoke(argv)


def test_scenario_U_planning_uses_dedicated_planner(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state.sessions["worker"].session_id is None
    assert state.sessions["reviewer"].session_id is None
    planner_id = state.sessions["planner"].session_id
    plan_reviewer_id = state.sessions["plan_reviewer"].session_id
    assert planner_id and plan_reviewer_id
    assert planner_id != plan_reviewer_id
    planners = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    workers = [inv for inv in provider.engine.invocations if inv.role == "worker"]
    assert planners
    assert not workers


def test_scenario_V_plan_revision_preserves_planning_sessions(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_revise("plan", "plan")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(4), provider)
    planners = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    reviewers = [inv for inv in provider.engine.invocations if inv.role == "plan_reviewer"]
    assert len(planners) >= 2 and len(reviewers) >= 2
    planner_id = provider.engine.sessions["planner"]
    reviewer_id = provider.engine.sessions["plan_reviewer"]
    assert all(inv.resume_session_id == planner_id for inv in planners[1:])
    assert all(inv.resume_session_id == reviewer_id for inv in reviewers[1:])


def test_scenario_W_execution_sessions_are_fresh(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    planner_id = state.sessions["planner"].session_id
    plan_reviewer_id = state.sessions["plan_reviewer"].session_id
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    final = load_lifecycle_state(repo)
    worker_id = final.sessions["worker"].session_id
    reviewer_id = final.sessions["reviewer"].session_id
    ids = [planner_id, plan_reviewer_id, worker_id, reviewer_id]
    assert all(ids)
    assert len(set(ids)) == 4
    assert worker_id != planner_id
    assert reviewer_id != plan_reviewer_id


def test_scenario_X_three_model_selections(tmp_path: Path):
    repo = make_repo(tmp_path)
    yaml_path = repo / ".ai" / "run.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8")
        + "\nmodels:\n  planner: P_MODEL\n  worker: W_MODEL\n  reviewer: R_MODEL\n",
        encoding="utf-8",
    )
    from auto_loop.manifest import load_run_manifest
    from auto_loop.run_inputs import prepare_repo_for_run

    loaded = prepare_repo_for_run(load_run_manifest(yaml_path)).config
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, build_run_options(loaded, max_turns=2), provider, config=loaded)
    baseline_before = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline_before, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, build_run_options(loaded, max_turns=2), provider)
    by_role = {inv.role: inv.model for inv in provider.engine.invocations}
    assert by_role["planner"] == "P_MODEL"
    assert by_role["plan_reviewer"] == "R_MODEL"
    assert by_role["worker"] == "W_MODEL"
    assert by_role["reviewer"] == "R_MODEL"


def test_scenario_Y_option_role_override_precedence(tmp_path: Path):
    repo = make_repo(tmp_path)
    loaded = frozen_config(repo)
    options = build_run_options(
        loaded,
        model="GLOBAL",
        reviewer_model="SPECIAL",
        max_turns=2,
    )
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, options, provider, config=loaded)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, options, provider)
    by_role = {inv.role: inv.model for inv in provider.engine.invocations}
    assert by_role["planner"] == "GLOBAL"
    assert by_role["worker"] == "GLOBAL"
    assert by_role["plan_reviewer"] == "SPECIAL"
    assert by_role["reviewer"] == "SPECIAL"


def test_scenario_Z_worker_updates_approved_plan(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = WorkerUpdatesPlanProvider(repo)
    approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    initial_hash = state.initial_approved_plan_sha256
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    final = load_lifecycle_state(repo)
    assert final.last_approved_commit == head
    assert final.current_plan_sha256 != initial_hash
    assert "worker plan update" in (repo / ".ai/auto-loop" / "plan.md").read_text(encoding="utf-8")
    assert provider.reviewer_prompts
    assert "plan changed since initial approval: yes" in provider.reviewer_prompts[-1]


def test_scenario_AA_worker_requests_execution_phase_plan_review(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    plan_reviewer_id = load_lifecycle_state(repo).sessions["plan_reviewer"].session_id
    baseline = load_lifecycle_state(repo).last_approved_commit
    plan = repo / ".ai/auto-loop" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nexecution update\n", encoding="utf-8")
    provider.set_response("worker", _plan_update_payload())
    provider.set_reviewer_revise("plan", "plan-update", slot="reviewer")
    provider.set_response("worker", _plan_update_payload())
    provider.set_reviewer_pass("plan", "plan-update", slot="reviewer")
    run_lifecycle(repo, run_opts(4), provider)
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == baseline
    exec_reviewers = [inv for inv in provider.engine.invocations if inv.role == "reviewer"]
    assert len(exec_reviewers) >= 2
    session_id = provider.engine.sessions["reviewer"]
    assert session_id
    assert all(inv.resume_session_id == session_id for inv in exec_reviewers[1:])
    assert provider.engine.sessions.get("plan_reviewer") == plan_reviewer_id
    planning_resumes = [
        inv
        for inv in provider.engine.invocations
        if inv.role == "plan_reviewer" and inv.resume_session_id == plan_reviewer_id
    ]
    assert len(planning_resumes) <= 1


def test_scenario_AB_preferred_single_revision_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "B production")
    provider.set_response("worker", batch_worker_payload(approved, head_b))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    head_c = commit_file(repo, "c.txt", "c\n", "C review-fix")
    provider.set_response("worker", batch_worker_payload(approved, head_c))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    (repo / "c.txt").write_text("c2\n", encoding="utf-8")
    git(repo, "add", "c.txt")
    git(repo, "commit", "--amend", "-m", "C2 amended review-fix")
    head_c2 = head_commit(repo)
    provider.set_response("worker", batch_worker_payload(approved, head_c2))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    history = subprocess.check_output(
        ["git", "rev-list", "--reverse", f"{approved}..HEAD"],
        cwd=repo,
        text=True,
    ).split()
    assert history == [head_b, head_c2]
    assert load_lifecycle_state(repo).last_approved_commit == head_c2
    reviews = sorted((repo / ".ai/auto-loop" / "reviews").glob("*.md"))
    rounds = [
        line.split(":", 1)[1].strip()
        for path in reviews
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- Round:")
    ]
    assert any(r == "2" or r == "3" for r in rounds)


def test_scenario_AC_multiple_revision_commits_still_accepted(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    commit_file(repo, "b.txt", "b\n", "B")
    commit_file(repo, "c.txt", "c\n", "C")
    head_d = commit_file(repo, "d.txt", "d\n", "D")
    provider.set_response("worker", batch_worker_payload(approved, head_d))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    assert load_lifecycle_state(repo).last_approved_commit == head_d
    count = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{approved}..HEAD"],
            cwd=repo,
            text=True,
        ).strip()
    )
    assert count == 3


def test_scenario_AD_noop_revision(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "B")
    provider.set_response("worker", batch_worker_payload(approved, head_b))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    pending = load_lifecycle_state(repo).pending_revision
    assert pending is not None
    assert pending.round == 2
    before = head_commit(repo)
    provider.set_response("worker", batch_worker_payload(approved, head_b))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    assert head_commit(repo) == before
    assert load_lifecycle_state(repo).last_approved_commit == head_b


def test_scenario_AE_gitignored_path_only_review(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore build")
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    build = repo / "build"
    build.mkdir()
    (build / "report.html").write_text("<html>ok</html>\n", encoding="utf-8")
    provider.set_response("worker", _path_batch_payload("build/report.html"))
    provider.set_response("reviewer", _reviewer_pass_targets("batch", "generated-validation", ["generated"]))
    run_lifecycle(repo, run_opts(2), provider)
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == approved
    reviews = sorted((repo / ".ai/auto-loop" / "reviews").glob("*.md"))
    text = reviews[-1].read_text(encoding="utf-8")
    assert "Fingerprint:" in text
    assert "build/report.html" in text


def test_scenario_AF_mixed_git_and_ignored_artifact_review(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore build")
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    approved = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    build = repo / "build"
    build.mkdir()
    (build / "report.html").write_text("<html>ok</html>\n", encoding="utf-8")
    provider.set_response(
        "worker",
        _path_batch_payload("build/report.html", base=approved, head=head),
    )
    provider.set_response(
        "reviewer",
        _reviewer_pass_targets("batch", "generated-validation", ["git"]),
    )
    missing = run_lifecycle(repo, run_opts(2), provider)
    assert missing.exit_code == ExitCode.PROTOCOL_ERROR
    provider.set_response(
        "reviewer",
        _reviewer_pass_targets("batch", "generated-validation", ["git", "generated"]),
    )
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert load_lifecycle_state(repo).last_approved_commit == head


def test_scenario_AG_dirty_tracked_source_cannot_bypass_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    (repo / "dirty.py").write_text("print('x')\n", encoding="utf-8")
    provider.set_response("worker", _path_batch_payload("dirty.py", target_id="dirty-src"))
    provider.set_response("worker", _path_batch_payload("dirty.py", target_id="dirty-src"))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    reviewers = [inv for inv in provider.engine.invocations if inv.role == "reviewer"]
    assert not reviewers


def test_scenario_AH_reviewer_mutates_plan(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ReviewerMutatesPlanProvider(repo)
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR


def test_scenario_AI_reviewer_mutates_requested_ignored_artifact(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore build")
    provider = ReviewerMutatesPathProvider(repo, "build/report.html")
    approve_plan(repo, provider)
    build = repo / "build"
    build.mkdir()
    (build / "report.html").write_text("<html>ok</html>\n", encoding="utf-8")
    provider.set_response("worker", _path_batch_payload("build/report.html"))
    provider.set_response("reviewer", _reviewer_pass_targets("batch", "generated-validation", ["generated"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR


def test_scenario_AJ_recovery_during_planning(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    run_lifecycle(repo, run_opts(1), provider)
    state = load_lifecycle_state(repo)
    planner_id = state.sessions["planner"].session_id
    assert planner_id
    state.inflight = InflightMarker(
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id=planner_id,
        started_at=utc_now(),
        head_before=head_commit(repo),
    )
    state.next_session = "plan_reviewer"
    save_lifecycle_state(repo, state)
    provider.engine.sessions["planner"] = planner_id
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(2), provider)
    planners = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    assert any(inv.resume_session_id == planner_id for inv in planners)
    reloaded = load_lifecycle_state(repo)
    assert reloaded.sessions["planner"].session_id == planner_id
    assert reloaded.inflight is None
    assert reloaded.plan_approved is True


def test_retired_planning_slots_reject_resume_after_handoff(tmp_path: Path):
    repo = make_repo(tmp_path)
    approve_plan(repo, ScriptedProvider())
    state = load_lifecycle_state(repo)
    state.next_session = "planner"
    save_lifecycle_state(repo, state)
    outcome = run_lifecycle(repo, run_opts(1), ScriptedProvider())
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    state.next_session = "plan_reviewer"
    save_lifecycle_state(repo, state)
    outcome = run_lifecycle(repo, run_opts(1), ScriptedProvider())
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


def test_scenario_AK_recovery_after_planning_handoff(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state.sessions["planner"].status == "retired"
    assert state.sessions["plan_reviewer"].status == "retired"
    planner_count = sum(1 for inv in provider.engine.invocations if inv.role == "planner")
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    worker_session = "worker-session-ak"
    state.sessions["worker"].session_id = worker_session
    state.inflight = InflightMarker(
        session_slot="worker",
        role="worker",
        turn=state.turn,
        session_id=worker_session,
        started_at=utc_now(),
        head_before=baseline,
    )
    state.next_session = "reviewer"
    save_lifecycle_state(repo, state)
    provider.engine.sessions["worker"] = worker_session
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    workers = [inv for inv in provider.engine.invocations if inv.role == "worker"]
    assert any(inv.resume_session_id == worker_session for inv in workers)
    assert sum(1 for inv in provider.engine.invocations if inv.role == "planner") == planner_count
    reloaded = load_lifecycle_state(repo)
    assert reloaded.sessions["planner"].status == "retired"
    assert reloaded.sessions["plan_reviewer"].status == "retired"
    assert reloaded.sessions["worker"].session_id == worker_session
