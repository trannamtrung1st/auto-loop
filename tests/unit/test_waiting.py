"""Bounded WAITING lifecycle regression tests (injected clock, no real sleeps)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import LifecycleStatus
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state
from tests.integration.scenario_harness import (
    approve_plan,
    execution_reviewer_invocation_count,
    make_repo,
    run_lifecycle,
    run_opts,
)


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sleeps = 0
        self.stop_on_sleep = False
        self.expire_runtime = False
        self.runner = None

    def bind_runner(self, runner) -> None:
        self.runner = runner

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: int) -> None:
        self.moment += timedelta(seconds=seconds)

    def sleep_until(self, when: datetime, abort):
        self.sleeps += 1
        if self.stop_on_sleep and self.runner is not None:
            self.runner._stop.requested = True
            return abort()
        if self.expire_runtime and self.runner is not None:
            self.runner._run_started_mono = time.monotonic() - 10**9
            return abort()
        self.moment = when
        return abort()


def _suspend_config(repo: Path, *, max_seconds: int = 1800, default_seconds: int = 60):
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "suspend"
    config.run.wait_default_seconds = default_seconds
    config.run.wait_max_seconds = max_seconds
    return config


def test_worker_waiting_reviewer_pass_enters_waiting(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    provider.set_worker_waiting("CI is running for the pushed closure SHA")
    provider.set_reviewer_pass("batch", "waiting")
    outcome = run_lifecycle(repo, run_opts(4), provider, config=_suspend_config(repo), clock=FakeClock())
    assert outcome.exit_code == ExitCode.WAITING
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status == LifecycleStatus.WAITING
    assert state.next_session == "worker"
    assert state.waiting_context is not None
    assert "CI is running" in state.waiting_context.reason
    assert execution_reviewer_invocation_count(provider) == 1
    assert sum(1 for inv in provider.engine.invocations if inv.role == "worker") == 1


def test_recheck_uses_same_worker_session_after_delay(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    reason = "CI is running for the pushed closure SHA"
    provider.set_worker_waiting(reason)
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    assert run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock).exit_code == ExitCode.WAITING
    session_id = load_lifecycle_state(repo).sessions["worker"].session_id
    deadline = load_lifecycle_state(repo).waiting_context.deadline_at
    clock.advance(60)
    provider.set_worker_waiting(reason)
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    state = load_lifecycle_state(repo)
    assert state.sessions["worker"].session_id == session_id
    assert state.waiting_context.deadline_at == deadline
    assert execution_reviewer_invocation_count(provider) == 1
    events = load_events(repo, config)
    assert any(event.get("type") == "waiting_recheck" for event in events)
    assert any(event.get("type") == "waiting_rescheduled" for event in events)


def test_recheck_does_not_run_before_next_check(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    workers_before = sum(1 for inv in provider.engine.invocations if inv.role == "worker")
    clock.advance(10)
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    workers_after = sum(1 for inv in provider.engine.invocations if inv.role == "worker")
    assert workers_after == workers_before


def test_changed_wait_evidence_requests_reviewer_again(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    reason = "CI is running"
    provider.set_worker_waiting(reason)
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    clock.advance(60)
    (repo / "new-evidence.txt").write_text("changed\n", encoding="utf-8")
    provider.set_worker_waiting(reason)
    provider.set_reviewer_pass("batch", "waiting")
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    assert execution_reviewer_invocation_count(provider) == 2


def test_waiting_resolves_into_review_request(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    run_lifecycle(repo, run_opts(6), provider, config=config, clock=clock)
    clock.advance(60)
    provider.set_worker_final_request()
    provider.set_reviewer_complete()
    outcome = run_lifecycle(repo, run_opts(6, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.COMPLETE
    events = load_events(repo, config)
    assert any(event.get("type") == "waiting_resolved" for event in events)


def test_waiting_recheck_can_become_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    clock.advance(60)
    provider.set_worker_blocked("CI failed and a human must rerun the workflow")
    provider.set_reviewer_blocked("operator must rerun CI")
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.BLOCKED


def test_wait_deadline_becomes_blocked(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo, max_seconds=30, default_seconds=10)
    run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    clock.advance(31)
    outcome = run_lifecycle(repo, run_opts(4, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.BLOCKED
    state = load_lifecycle_state(repo)
    assert state.status == LifecycleStatus.BLOCKED
    assert any(event.get("type") == "waiting_timeout" for event in load_events(repo, config))


def test_auto_wait_sleeps_without_extra_provider_calls_and_stop_interrupts(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    clock.stop_on_sleep = True
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "auto"
    outcome = run_lifecycle(repo, run_opts(6), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.STOPPED
    assert clock.sleeps == 1
    assert sum(1 for inv in provider.engine.invocations if inv.role == "worker") == 1
    state = load_lifecycle_state(repo)
    assert state.status == LifecycleStatus.STOPPED
    assert state.waiting_context is not None


def test_max_runtime_still_applies_during_wait(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    clock.expire_runtime = True
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "auto"
    outcome = run_lifecycle(repo, run_opts(6), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_planner_waiting_and_git_mode_off(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    bootstrap_workspace(repo, git_mode="off")
    provider = ScriptedProvider()
    clock = FakeClock()
    provider.set_planner_waiting("external spec review is already in progress")
    provider.set_reviewer_pass("plan", "waiting", slot="plan_reviewer")
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "suspend"
    outcome = run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    assert load_lifecycle_state(repo).next_session == "planner"


def test_turn_budget_exhausted_while_waiting_stays_waiting(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "auto"
    outcome = run_lifecycle(repo, run_opts(2), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    state = load_lifecycle_state(repo)
    assert state.status == LifecycleStatus.WAITING
    assert clock.sleeps == 0
    assert sum(1 for inv in provider.engine.invocations if inv.role == "worker") == 1


def test_resume_after_stop_during_wait_honors_schedule(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    clock.stop_on_sleep = True
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    manifest = load_run_manifest(repo / ".ai/run.yaml")
    config = manifest.config
    config.run.wait_mode = "auto"
    assert run_lifecycle(repo, run_opts(6), provider, config=config, clock=clock).exit_code == ExitCode.STOPPED
    clock.stop_on_sleep = False
    config.run.wait_mode = "suspend"
    outcome = run_lifecycle(repo, run_opts(2, resuming=True), provider, config=config, clock=clock)
    assert outcome.exit_code == ExitCode.WAITING
    assert sum(1 for inv in provider.engine.invocations if inv.role == "worker") == 1


def test_restart_preserves_wait_deadline(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    clock = FakeClock()
    provider.set_worker_waiting("CI is running")
    provider.set_reviewer_pass("batch", "waiting")
    config = _suspend_config(repo)
    run_lifecycle(repo, run_opts(4), provider, config=config, clock=clock)
    first = load_lifecycle_state(repo).waiting_context
    clock2 = FakeClock()
    clock2.moment = clock.moment
    outcome = run_lifecycle(repo, run_opts(2, resuming=True), provider, config=config, clock=clock2)
    assert outcome.exit_code == ExitCode.WAITING
    second = load_lifecycle_state(repo).waiting_context
    assert second.deadline_at == first.deadline_at
    assert second.next_check_at == first.next_check_at
    assert second.first_waited_at == first.first_waited_at
