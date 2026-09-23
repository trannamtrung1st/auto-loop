"""Proposal section 44 session, recovery, safety, blocker, and limit scenarios K-T."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.lifecycle import InflightMarker, LifecycleStatus, utc_now
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.fake_cursor import FakeBehavior
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import load_completion_record
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    approve_plan,
    batch_worker_payload,
    commit_file,
    make_repo,
    run_opts,
)


class CrashOnceScriptedProvider(ScriptedProvider):
    """Simulates one provider crash then success on the same prepared response."""

    def __init__(self) -> None:
        super().__init__()
        self._crash_pending = True

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if self._crash_pending:
            self._crash_pending = False
            self.engine.behavior = FakeBehavior.CRASH
            try:
                return self.engine.run(argv)
            finally:
                self.engine.behavior = FakeBehavior.OK
        return super().invoke(argv)


class SessionMismatchOnSecondWorkerProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self._worker_calls = 0

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "worker":
            self._worker_calls += 1
            if self._worker_calls >= 2 and self.engine.sessions.get("worker"):
                self.engine.behavior = FakeBehavior.SESSION_MISMATCH
        return super().invoke(argv)


class ReviewerMutatesProductProvider(ScriptedProvider):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self._reviewer_calls = 0

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "reviewer":
            (self.repo / "reviewer_touch.txt").write_text("mutated\n", encoding="utf-8")
        return super().invoke(argv)


class WorkerMutatesTaskProvider(ScriptedProvider):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") in ("worker", "planner"):
            task = self.repo / ".ai/auto-loop" / "task.md"
            task.write_text(task.read_text(encoding="utf-8") + "\nworker edit\n", encoding="utf-8")
        return super().invoke(argv)


def test_scenario_K_persistent_planner_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_revise("plan", "plan")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(4), provider)
    planners = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    assert len(planners) >= 2
    session_id = provider.engine.sessions["planner"]
    assert session_id
    assert all(inv.resume_session_id == session_id for inv in planners[1:])
    state = load_lifecycle_state(repo)
    assert state.sessions["planner"].session_id == session_id
    assert state.sessions["worker"].session_id is None


def test_scenario_L_persistent_plan_reviewer_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_revise("plan", "plan")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(4), provider)
    reviewers = [inv for inv in provider.engine.invocations if inv.role == "plan_reviewer"]
    assert len(reviewers) >= 2
    session_id = provider.engine.sessions["plan_reviewer"]
    assert session_id
    assert all(inv.resume_session_id == session_id for inv in reviewers[1:])
    state = load_lifecycle_state(repo)
    assert state.sessions["plan_reviewer"].session_id == session_id
    assert state.sessions["reviewer"].session_id is None


def test_scenario_M_session_mismatch_returns_session_error(tmp_path: Path):
    repo = make_repo(tmp_path)
    approve_plan(repo, ScriptedProvider())
    provider = SessionMismatchOnSecondWorkerProvider()
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    expected_session = load_lifecycle_state(repo).sessions["worker"].session_id
    assert expected_session
    provider.set_response("worker", batch_worker_payload(baseline, head))
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.SESSION_ERROR
    reloaded = load_lifecycle_state(repo)
    assert reloaded.sessions["worker"].session_id == expected_session


def test_scenario_N_controller_interruption_reconciles_existing_commit(tmp_path: Path):
    repo = make_repo(tmp_path)
    approve_plan(repo, ScriptedProvider())
    provider = PromptCapturingProvider()
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    state = load_lifecycle_state(repo)
    worker_session = state.sessions["worker"].session_id or "worker-session-n"
    state.sessions["worker"].session_id = worker_session
    state.inflight = InflightMarker(
        session_slot="worker",
        role="worker",
        turn=state.turn,
        session_id=worker_session,
        started_at=utc_now(),
        head_before=baseline,
    )
    state.next_actor = "worker"
    save_lifecycle_state(repo, state)
    provider.engine.sessions["worker"] = worker_session
    reviewer_session = state.sessions["reviewer"].session_id
    if reviewer_session:
        provider.engine.sessions["reviewer"] = reviewer_session
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    assert provider.worker_prompts
    assert "interrupted" in provider.worker_prompts[-1].lower()
    count = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{baseline}..{head}"],
            cwd=repo,
            text=True,
        ).strip()
    )
    assert count == 1
    assert load_lifecycle_state(repo).last_approved_commit == head


def test_scenario_O_provider_crash_retries_same_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = CrashOnceScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    planner_invocations = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    assert len(planner_invocations) >= 2
    session_id = provider.engine.sessions["planner"]
    assert session_id
    assert all(
        inv.resume_session_id in (None, session_id) for inv in planner_invocations[:2]
    )
    assert load_lifecycle_state(repo).sessions["planner"].session_id == session_id


def test_scenario_P_protocol_failure_repairs_same_session(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    provider.set_invalid_protocol_response("planner")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(2), provider)
    assert provider.worker_prompts
    repair_prompt = provider.worker_prompts[-1]
    assert "AUTO_LOOP_RESULT" in repair_prompt or "valid AUTO_LOOP_RESULT" in repair_prompt
    planner_session = load_lifecycle_state(repo).sessions["planner"].session_id
    assert planner_session
    resumes = [
        inv.resume_session_id or ""
        for inv in provider.engine.invocations
        if inv.role == "planner"
    ]
    assert resumes.count(planner_session) >= 1


def test_scenario_Q_reviewer_product_mutation_invalidates_verdict(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ReviewerMutatesProductProvider(repo)
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "feature")
    provider.set_response("worker", batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    assert load_lifecycle_state(repo).last_approved_commit == baseline


def test_scenario_R_protected_task_mutation_stops_without_revert(tmp_path: Path):
    repo = make_repo(tmp_path)
    task = repo / ".ai/auto-loop" / "task.md"
    before = task.read_text(encoding="utf-8")
    provider = WorkerMutatesTaskProvider(repo)
    provider.set_worker_plan_request()
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.PROTECTION_VIOLATION
    after = task.read_text(encoding="utf-8")
    assert after != before
    assert "worker edit" in after


def test_scenario_S_blocked_worker_reviewer_revise_resumes_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_revise("batch", "blocked")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    state = load_lifecycle_state(repo)
    assert state.next_actor == "worker"
    assert not (repo / ".ai/auto-loop/runtime/blocked.json").is_file()
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_scenario_S_blocked_worker_reviewer_blocked_writes_record(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    blocked = json.loads((repo / ".ai/auto-loop/runtime/blocked.json").read_text(encoding="utf-8"))
    assert blocked["status"] == "blocked"
    assert blocked["resume_session"] == "worker"
    assert blocked["blocked_by_session"] == "reviewer"


def test_scenario_S_blocked_reviewer_resume_worker(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_blocked()
    provider.set_reviewer_blocked("external CI gate")
    assert run_lifecycle(repo, run_opts(3), provider).exit_code == ExitCode.BLOCKED
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(3, resuming=True), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    assert load_lifecycle_state(repo).status == LifecycleStatus.COMPLETED


def test_scenario_T_limit_reached_never_complete(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    for _ in range(4):
        provider.set_worker_plan_request()
        provider.set_reviewer_revise("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state.status == LifecycleStatus.LIMIT_REACHED
    assert load_completion_record(repo) is None
