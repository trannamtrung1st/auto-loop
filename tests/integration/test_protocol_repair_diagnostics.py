"""Protocol repair prompts and durable diagnostics."""

from __future__ import annotations

from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.runtime import load_lifecycle_state
from tests.integration.scenario_harness import (
    PromptCapturingProvider,
    batch_worker_payload,
    commit_file,
    make_repo,
    run_lifecycle,
    run_opts,
)
from tests.integration.test_lifecycle_flows import _approve_plan
from tests.repo_utils import bootstrapped_manifest, frozen_config


def test_worker_invalid_status_repair_prompt(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    state = load_lifecycle_state(repo)
    assert state is not None
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "worker change")
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "implementing",
            "work_summary": "still working",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_response("worker", batch_worker_payload(baseline, head, "W01"))
    provider.set_reviewer_pass("batch", "W01")
    provider.set_worker_blocked()

    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    repair_prompts = [
        prompt
        for prompt in provider.worker_prompts
        if "Your previous work is preserved" in prompt
    ]
    assert repair_prompts
    repair = repair_prompts[0]
    assert "implementing" in repair
    assert "review_requested" in repair
    assert "controller rejected it" not in repair.lower()
    assert "Reconcile this durable state" not in repair


def test_protocol_repair_exhaustion_persists_failure_and_status(tmp_path: Path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"run": cfg.run.model_copy(update={"protocol_retries": 0})})
    source = bootstrapped_manifest(repo)
    monkeypatch.setattr("auto_loop.loop.ensure_run_prerequisites", lambda _repo, _config: None)

    provider = PromptCapturingProvider()
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "implementing",
            "work_summary": "still working",
            "verification": [],
            "notes": [],
        },
    )
    _approve_plan(repo, provider)

    from auto_loop.loop import run_lifecycle as core_run_lifecycle

    outcome = core_run_lifecycle(
        repo,
        run_opts(1),
        provider,
        config=cfg,
        artifact_root=source.artifact_root,
    )
    assert outcome.exit_code == ExitCode.PROTOCOL_ERROR
    state = load_lifecycle_state(repo, source.artifact_root)
    assert state is not None
    assert state.last_run_failure is not None
    assert state.last_run_failure.session == "worker"
    assert "implementing" in state.last_run_failure.repair_reason
    assert state.inflight is not None
    assert state.inflight.repair_reason
    assert "implementing" in state.inflight.repair_reason

    from auto_loop.manifest import load_run_manifest
    from auto_loop.status_report import build_status_report

    report = build_status_report(load_run_manifest(source.path))
    assert "interrupted · protocol error" in report
    assert "repair exhausted" in report
    assert "implementing" in report


def test_protocol_exhaustion_resume_corrects_handoff_end_to_end(
    tmp_path: Path, monkeypatch
):
    """PROTOCOL_ERROR exit, then explicit resume repairs output and continues review."""
    repo = make_repo(tmp_path)
    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"run": cfg.run.model_copy(update={"protocol_retries": 0})})
    source = bootstrapped_manifest(repo)
    monkeypatch.setattr("auto_loop.loop.ensure_run_prerequisites", lambda _repo, _config: None)

    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    state = load_lifecycle_state(repo, source.artifact_root)
    assert state is not None
    baseline = state.last_approved_commit
    head = commit_file(repo, "feature.txt", "x\n", "worker change")

    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "implementing",
            "work_summary": "still working",
            "verification": [],
            "notes": [],
        },
    )

    from auto_loop.loop import run_lifecycle as core_run_lifecycle

    first = core_run_lifecycle(
        repo,
        run_opts(1),
        provider,
        config=cfg,
        artifact_root=source.artifact_root,
    )
    assert first.exit_code == ExitCode.PROTOCOL_ERROR

    state = load_lifecycle_state(repo, source.artifact_root)
    assert state is not None
    worker_session = state.sessions["worker"].session_id
    assert worker_session
    assert state.last_run_failure is not None
    assert state.inflight is not None
    stored_reason = state.inflight.repair_reason
    assert stored_reason and "implementing" in stored_reason
    prompts_after_failure = len(provider.worker_prompts)

    provider.set_response("worker", batch_worker_payload(baseline, head, "W01"))
    provider.set_reviewer_pass("batch", "W01")

    second = run_lifecycle(
        repo,
        run_opts(2, resuming=True),
        provider,
        config=cfg,
        artifact_root=source.artifact_root,
    )
    assert second.exit_code == ExitCode.LIMIT_REACHED
    assert len(provider.worker_prompts) > prompts_after_failure

    resume_prompt = provider.worker_prompts[prompts_after_failure]
    assert "Your previous work is preserved" in resume_prompt
    assert "implementing" in resume_prompt
    assert "review_requested" in resume_prompt
    assert "Do not perform implementation work" in resume_prompt
    for line in stored_reason.splitlines():
        if line.strip().startswith("status:"):
            assert line.strip() in resume_prompt

    worker_invocations = [
        inv for inv in provider.engine.invocations if inv.role == "worker"
    ]
    assert len(worker_invocations) == 2
    assert worker_invocations[-1].resume_session_id == worker_session

    reviewer_invocations = [
        inv for inv in provider.engine.invocations if inv.role == "reviewer"
    ]
    assert len(reviewer_invocations) == 1
    assert reviewer_invocations[0].resume_session_id in (
        None,
        state.sessions["reviewer"].session_id,
    )

    state = load_lifecycle_state(repo, source.artifact_root)
    assert state is not None
    assert state.last_run_failure is None
    assert state.consecutive_protocol_failures == 0
    assert state.sessions["worker"].session_id == worker_session
    assert state.inflight is None
    reviews = sorted((repo / cfg.reviews_dir).glob("*.md"))
    assert reviews
    assert "W01" in reviews[-1].read_text(encoding="utf-8")
