"""Opt-in real Cursor lifecycle smoke test (proposal section 45)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_loop.exits import ExitCode
from auto_loop.live_smoke import (
    assert_smoke_success,
    collect_smoke_evidence,
    live_smoke_gate,
    prepare_smoke_repository,
)
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.run_options import RunOptions
from tests.repo_utils import frozen_config


def test_live_cursor_smoke_skipped_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_LOOP_LIVE_CURSOR", raising=False)
    gate = live_smoke_gate()
    assert not gate.enabled
    assert "AUTO_LOOP_LIVE_CURSOR" in gate.skip_reason


@pytest.mark.live_cursor
def test_live_cursor_smoke_lifecycle(tmp_path: Path):
    gate = live_smoke_gate()
    if not gate.enabled:
        pytest.skip(gate.skip_reason)

    repo = tmp_path / "smoke-repo"
    prepare_smoke_repository(repo)
    config = frozen_config(repo)
    options = RunOptions(
        config.agents["worker"].model,
        config.agents["reviewer"].model,
        max_turns=30,
        max_runtime_minutes=120,
        verbose=True,
        quiet=False,
    )
    outcome = run_lifecycle(repo, options, SubprocessCursorProvider(config))
    evidence = collect_smoke_evidence(repo, outcome.exit_code)
    evidence_path = tmp_path / "smoke-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "exit_code": evidence.exit_code,
                "worker_session_id": evidence.worker_session_id,
                "reviewer_session_id": evidence.reviewer_session_id,
                "review_count": evidence.review_count,
                "notes": evidence.notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if outcome.exit_code != ExitCode.COMPLETE:
        pytest.fail(
            f"Live smoke did not complete (exit={outcome.exit_code}); "
            f"evidence={evidence_path}; notes={evidence.notes}"
        )
    assert_smoke_success(evidence)
