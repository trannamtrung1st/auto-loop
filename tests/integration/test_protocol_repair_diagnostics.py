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
        if "controller rejected it" in prompt.lower()
    ]
    assert repair_prompts
    repair = repair_prompts[0]
    assert "implementing" in repair
    assert "review_requested" in repair
    assert "did not produce a valid AUTO_LOOP_RESULT" not in repair


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
