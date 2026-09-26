"""Status report rendering."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import (
    ActiveReview,
    HistoryReconciliation,
    InflightMarker,
    LifecycleStatus,
    create_lifecycle,
)
from auto_loop.manifest import load_run_manifest
from auto_loop.runtime import save_lifecycle_state
from auto_loop.status_report import build_status_report, resume_progress_text


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    bootstrap_workspace(repo, minimal=True)
    return repo


def test_status_reports_core_fields(tmp_path: Path):
    repo = _repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    state = create_lifecycle(head)
    state.sessions["worker"].session_id = "worker-session-uuid-0001"
    state.sessions["reviewer"].session_id = "reviewer-session-uuid-0002"
    save_lifecycle_state(repo, state)
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "status: running" in report
    assert "next session: planner" in report
    assert "planner session:" in report
    assert "plan-reviewer session:" in report
    assert "worker session:" in report
    assert "phase: planning" in report
    assert "HEAD:" in report
    assert "product tree:" in report


def test_resume_progress_text_distinguishes_running_final_handoff():
    state = create_lifecycle("abc")
    state.phase = "execution"
    state.status = LifecycleStatus.RUNNING
    state.turn = 356
    state.next_session = "worker"
    state.plan_approved = True
    state.inflight = InflightMarker(
        session_slot="worker",
        role="worker",
        turn=356,
        session_id="sess",
        started_at=datetime.now(timezone.utc),
    )
    state.active_review = ActiveReview(
        cycle_id="review-0001",
        scope="final",
        target="whole-task",
        summary="final",
    )
    assert resume_progress_text(state) == "\n".join(
        [
            "Lifecycle resumed",
            "Phase: final handoff",
            "Session: worker",
            "Turn: 356",
            "Status: RUNNING",
        ]
    )


def test_status_reports_history_reconciliation_decision(tmp_path: Path):
    repo = _repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    state = create_lifecycle(head)
    state.history_reconciliation = HistoryReconciliation(
        old_sha="a" * 40,
        new_sha=None,
        head_sha=head,
        evidence=["ambiguous"],
        candidates=["b" * 40, "c" * 40],
        applied=False,
        needs_decision=True,
    )
    save_lifecycle_state(repo, state)
    report = build_status_report(load_run_manifest(repo / ".ai" / "run.yaml"))
    assert "Run:        NEEDS HISTORY RECONCILIATION" in report
    assert "status: NEEDS HISTORY RECONCILIATION" in report
    assert "approved baseline: unchanged" in report
    assert "bbbbbbb" in report
