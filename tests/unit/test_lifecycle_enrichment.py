"""load_lifecycle_state enrichment must not mutate v2 review snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from auto_loop.config import dump_config
from tests.repo_utils import frozen_config
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import (
    LEGACY_V1_PLAN_TARGET_PATH,
    ActiveReview,
    LifecycleState,
    SessionRecord,
    create_lifecycle,
)
from auto_loop.models import ActivePathTarget
from auto_loop.review_targets import fingerprint_path, verify_path_targets_unchanged
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state, state_path

from tests.unit.test_lifecycle import _repo


def _plan_target(fingerprint: str, path: str = ".ai/auto-loop/plan.md") -> ActivePathTarget:
    return ActivePathTarget(
        id="plan",
        path=path,
        fingerprint=fingerprint,
        exists=True,
        git_classification="control",
        purpose="plan",
    )


def _execution_plan_review_state(repo: Path, plan_target: ActivePathTarget) -> LifecycleState:
    from auto_loop.git import head_commit

    head = head_commit(repo)
    state = create_lifecycle(head)
    state.plan_approved = True
    state.phase = "execution"
    state.next_session = "reviewer"
    state.sessions = {
        "planner": SessionRecord(role="planner", status="retired"),
        "plan_reviewer": SessionRecord(role="reviewer", status="retired"),
        "worker": SessionRecord(role="worker", session_id="w1"),
        "reviewer": SessionRecord(role="reviewer", session_id="r1"),
    }
    state.active_review = ActiveReview(
        cycle_id="review-0001",
        scope="plan",
        target="plan-update",
        summary="review plan",
        session_purpose="reviewer",
        targets=[plan_target],
    )
    return state


def test_reload_preserves_v2_plan_fingerprint_after_plan_changes(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    plan_path = repo / ".ai/auto-loop" / "plan.md"
    f1, _ = fingerprint_path(plan_path)
    state = _execution_plan_review_state(repo, _plan_target(f1))
    save_lifecycle_state(repo, state)

    plan_path.write_text("# changed plan content\n", encoding="utf-8")
    f2, _ = fingerprint_path(plan_path)
    assert f1 != f2

    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    stored = next(t for t in loaded.active_review.targets if t.id == "plan")
    assert stored.fingerprint == f1

    violations = verify_path_targets_unchanged(repo, [stored])
    assert violations


def test_reload_does_not_retarget_v2_plan_when_frozen_artifact_root_changes(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    plan_path = repo / ".ai/auto-loop" / "plan.md"
    digest, _ = fingerprint_path(plan_path)
    state = _execution_plan_review_state(repo, _plan_target(digest, ".ai/auto-loop/plan.md"))
    save_lifecycle_state(repo, state)

    cfg = frozen_config(repo)
    cfg = cfg.model_copy(update={"artifacts": cfg.artifacts.model_copy(update={"root": ".ai/other-loop"})})
    snapshot = repo / ".ai/auto-loop" / "runtime" / "config.resolved.yaml"
    snapshot.write_text(dump_config(cfg), encoding="utf-8")

    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    stored = next(t for t in loaded.active_review.targets if t.id == "plan")
    assert stored.path == ".ai/auto-loop/plan.md"
    assert stored.fingerprint == digest


def test_empty_fingerprint_on_non_legacy_path_target_is_not_enriched(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    artifact = repo / "build" / "report.html"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("<html>v1</html>\n", encoding="utf-8")
    on_disk, _ = fingerprint_path(artifact)

    state = _execution_plan_review_state(
        repo,
        ActivePathTarget(
            id="artifact",
            path="build/report.html",
            fingerprint="",
            exists=True,
            git_classification="ignored",
            purpose="report",
        ),
    )
    state.active_review = state.active_review.model_copy(
        update={"scope": "batch", "target": "W01"}
    )
    save_lifecycle_state(repo, state)

    artifact.write_text("<html>changed</html>\n", encoding="utf-8")
    changed_on_disk, _ = fingerprint_path(artifact)
    assert on_disk != changed_on_disk

    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    stored = next(t for t in loaded.active_review.targets if t.id == "artifact")
    assert stored.fingerprint == ""
    assert stored.path == "build/report.html"


def test_legacy_sentinel_still_enriches_on_first_load(tmp_path: Path):
    repo = _repo(tmp_path)
    bootstrap_workspace(repo)
    from auto_loop.git import head_commit

    head = head_commit(repo)
    v1 = {
        "schema_version": 1,
        "lifecycle_id": "x",
        "status": "running",
        "turn": 1,
        "next_actor": "reviewer",
        "plan_approved": True,
        "initial_base_commit": head,
        "last_approved_commit": head,
        "sessions": {
            "worker": {"session_id": "w", "model": "auto"},
            "reviewer": {"session_id": "r", "model": "auto"},
        },
        "active_review": {"scope": "plan", "target": "plan", "summary": "s"},
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    state_path(repo).parent.mkdir(parents=True, exist_ok=True)
    state_path(repo).write_text(json.dumps(v1), encoding="utf-8")

    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    stored = next(t for t in loaded.active_review.targets if t.id == "plan")
    assert stored.path != LEGACY_V1_PLAN_TARGET_PATH
    assert stored.fingerprint
