"""End-to-end v1 execution plan-review migration."""

from __future__ import annotations

import json
from pathlib import Path

from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.review_targets import fingerprint_path
from auto_loop.runtime import load_lifecycle_state, state_path

from tests.integration.scenario_harness import make_repo, run_opts


def test_migrated_v1_plan_review_passes_without_review_mutation_error(tmp_path: Path):
    repo = make_repo(tmp_path)
    head = head_commit(repo)
    plan_rel = ".auto-loop/plan.md"
    digest, exists = fingerprint_path(repo / plan_rel)
    assert exists

    v1_state = {
        "schema_version": 1,
        "lifecycle_id": "legacy-plan-review",
        "status": "running",
        "turn": 7,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": head,
        "last_approved_commit": head,
        "sessions": {
            "worker": {"session_id": "w1", "model": "auto"},
            "reviewer": {"session_id": "r1", "model": "auto"},
        },
        "active_review": {
            "scope": "plan",
            "target": "plan-update",
            "summary": "worker amended plan",
        },
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    state_path(repo).write_text(json.dumps(v1_state), encoding="utf-8")

    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    assert loaded.active_review is not None
    plan_target = next(t for t in loaded.active_review.targets if t.id == "plan")
    assert plan_target.fingerprint == digest

    provider = ScriptedProvider()
    provider.set_reviewer_pass("plan", "plan-update", slot="reviewer")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code != ExitCode.REVIEW_MUTATION_ERROR
