"""Planner product-policy failures are discarded, not replayed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_loop.events import load_events
from auto_loop.exits import ExitCode
from auto_loop.git import PLANNING_POLICY_PREFIX, head_commit
from auto_loop.lifecycle import CompletedProviderTurn
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.status_report import build_status_report
from tests.integration.scenario_harness import git, make_repo, run_lifecycle, run_opts
from tests.repo_utils import frozen_config


class _ProductMutatingPlanReviewer(ScriptedProvider):
    """Commit a product file during plan review, and optionally reset on the next planner turn."""

    def __init__(self, repo: Path, *, reset_to: str | None = None) -> None:
        super().__init__()
        self.repo = repo
        self.reset_to = reset_to
        self.mutated = False
        self.did_reset = False

    def invoke(self, argv: list[str]):
        slot = os.environ.get("AUTO_LOOP_FAKE_SLOT")
        if slot == "plan_reviewer" and not self.mutated:
            self.mutated = True
            path = self.repo / "unexpected.txt"
            path.write_text("unexpected\n", encoding="utf-8")
            git(self.repo, "add", "unexpected.txt")
            git(self.repo, "commit", "-m", "unexpected during plan review")
        if slot == "planner" and self.reset_to and self.mutated and not self.did_reset:
            self.did_reset = True
            git(self.repo, "reset", "--hard", self.reset_to)
            plan = self.repo / ".ai" / "auto-loop" / "plan.md"
            plan.write_text("# revised after restore\n", encoding="utf-8")
        return super().invoke(argv)


def _event_types(repo: Path) -> list[str]:
    return [event.get("type", "") for event in load_events(repo, frozen_config(repo))]


def _mutate_plan_review(repo: Path, provider: _ProductMutatingPlanReviewer) -> str:
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "planner"
    assert head_commit(repo) != state.initial_base_commit
    assert state.initial_base_commit
    return state.initial_base_commit


def _inject_failed_planner_turn(
    repo: Path,
    *,
    product_head_before: str,
    transition_error: str | None = None,
) -> None:
    state = load_lifecycle_state(repo)
    assert state is not None
    state.completed_provider_turn = CompletedProviderTurn(
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id=state.sessions["planner"].session_id,
        result_kind="planner",
        result={
            "schema_version": 2,
            "actor": "planner",
            "status": "review_requested",
            "review": {"scope": "plan", "target": "plan", "summary": "STALE PLANNER"},
            "plan_summary": "STALE PLANNER",
            "notes": [],
        },
        transition_error=transition_error
        or (
            f"{PLANNING_POLICY_PREFIX} Planner mutated product files outside the artifact root"
        ),
        product_head_before=product_head_before,
        product_changes_before=[],
    )
    state.inflight = None
    state.next_session = "planner"
    save_lifecycle_state(repo, state)


def test_planner_reset_to_baseline_discards_turn_and_continues(tmp_path: Path):
    repo = make_repo(tmp_path)
    baseline = head_commit(repo)
    provider = _ProductMutatingPlanReviewer(repo, reset_to=baseline)
    _mutate_plan_review(repo, provider)
    provider.set_planner_review_request()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")

    start = len(provider.engine.invocations)
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert "state.json" not in (outcome.message or "")

    new = provider.engine.invocations[start:]
    planner = [inv for inv in new if inv.role == "planner"]
    assert len(planner) == 2
    assert "Do not repair or revert product Git state." in planner[0].prompt
    assert "repair or revert it before" not in planner[0].prompt.lower()
    assert "Planner turn discarded" in planner[1].prompt
    assert "product Git state must remain unchanged" in planner[1].prompt
    assert "repair or revert it before" not in planner[1].prompt.lower()
    assert planner[1].resume_session_id
    assert planner[1].resume_session_id == planner[0].resume_session_id

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.phase == "execution"
    assert state.completed_provider_turn is None
    assert head_commit(repo) == baseline
    assert not (repo / "unexpected.txt").exists()
    discarded = [
        event
        for event in load_events(repo, frozen_config(repo))
        if event.get("type") == "planner_turn_discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0]["product_head_before"] != baseline
    assert discarded[0]["product_head_after"] == baseline
    assert "Planner turn discarded" in discarded[0]["message"]


def test_resume_skips_replay_until_head_returns_to_baseline(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _ProductMutatingPlanReviewer(repo)
    baseline = _mutate_plan_review(repo, provider)
    observed = head_commit(repo)
    _inject_failed_planner_turn(repo, product_head_before=observed)

    provider.set_planner_review_request()
    start = len(provider.engine.invocations)
    stuck = run_lifecycle(repo, run_opts(2), provider)
    assert stuck.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    assert stuck.message is not None
    assert baseline in stuck.message
    assert observed in stuck.message
    assert "cannot be replayed" in stuck.message
    assert "state.json" not in stuck.message
    assert len(provider.engine.invocations) == start

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is not None
    assert state.completed_provider_turn.result["plan_summary"] == "STALE PLANNER"
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "planner recovery:" in report
    assert "not replayed" in report
    assert f"expected planning HEAD: {baseline[:7]}" in report
    assert "state.json" not in report

    git(repo, "reset", "--hard", baseline)
    provider.set_reviewer_pass("plan", "plan")
    resumed = run_lifecycle(repo, run_opts(2), provider)
    assert resumed.exit_code == ExitCode.LIMIT_REACHED
    fresh = provider.engine.invocations[start:]
    planner = [inv for inv in fresh if inv.role == "planner"]
    assert len(planner) == 1
    assert "STALE PLANNER" not in planner[0].prompt
    assert "Planner turn discarded" in planner[0].prompt
    assert "state.json" not in planner[0].prompt
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.completed_provider_turn is None
    assert head_commit(repo) == baseline
    reviews = list((repo / ".ai" / "auto-loop" / "reviews").glob("*.md"))
    assert reviews
    assert "STALE PLANNER" not in reviews[-1].read_text(encoding="utf-8")


def test_crash_after_discard_before_reopen_resumes_fresh_planner(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    baseline = head_commit(repo)
    provider = _ProductMutatingPlanReviewer(repo, reset_to=baseline)
    _mutate_plan_review(repo, provider)
    provider.set_planner_review_request()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")

    from auto_loop.loop import LifecycleRunner

    original = LifecycleRunner._persist_inflight

    def crash_on_reopen(self, state, slot, **kwargs):
        reason = kwargs.get("repair_reason") or ""
        if "Planner turn discarded" in reason:
            raise RuntimeError("crash before reopen")
        return original(self, state, slot, **kwargs)

    monkeypatch.setattr(LifecycleRunner, "_persist_inflight", crash_on_reopen)
    with pytest.raises(RuntimeError, match="crash before reopen"):
        run_lifecycle(repo, run_opts(2), provider)
    monkeypatch.setattr(LifecycleRunner, "_persist_inflight", original)

    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is None
    assert state.inflight is None
    assert state.next_session == "planner"
    assert head_commit(repo) == baseline
    assert "planner_turn_discarded" in _event_types(repo)

    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.phase == "execution"


def test_repeated_planner_mutation_hits_protocol_retry_limit(tmp_path: Path):
    repo = make_repo(tmp_path)
    baseline = head_commit(repo)
    provider = _ProductMutatingPlanReviewer(repo, reset_to=baseline)
    _mutate_plan_review(repo, provider)
    provider.set_planner_review_request()
    provider.set_planner_review_request()

    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"run": cfg.run.model_copy(update={"protocol_retries": 0})})
    start = len(provider.engine.invocations)
    outcome = run_lifecycle(repo, run_opts(3), provider, config=cfg)
    assert outcome.exit_code == ExitCode.PROTOCOL_ERROR
    assert outcome.message is not None
    assert "Planner turn discarded" in outcome.message
    assert "Protocol repair exhausted." in outcome.message
    assert "state.json" not in outcome.message
    planner = [inv for inv in provider.engine.invocations[start:] if inv.role == "planner"]
    assert len(planner) == 1
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.completed_provider_turn is None
    assert state.last_run_failure is not None
    assert state.last_run_failure.session == "planner"
    assert state.inflight is not None
    assert state.inflight.repair_reason is not None
    assert "Planner turn discarded" in state.inflight.repair_reason
    assert state.plan_approved is False
    assert "planner_turn_discarded" in _event_types(repo)


def test_legacy_unprefixed_policy_error_is_not_replayed(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = _ProductMutatingPlanReviewer(repo)
    baseline = _mutate_plan_review(repo, provider)
    observed = head_commit(repo)
    _inject_failed_planner_turn(
        repo,
        product_head_before=observed,
        transition_error="Planner mutated product files outside the artifact root",
    )
    git(repo, "reset", "--hard", baseline)
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.completed_provider_turn is None
    assert "planner_turn_discarded" in _event_types(repo)
