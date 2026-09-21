"""Status report rendering."""

import subprocess
from pathlib import Path

from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import create_lifecycle
from auto_loop.manifest import load_run_manifest
from auto_loop.runtime import save_lifecycle_state
from auto_loop.status_report import build_status_report


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
