"""Terminal completion record helpers."""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from auto_loop.config import load_config_from_repo
from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.terminal_records import (
    CompletionRecord,
    completion_still_valid,
    save_completion_record,
    task_and_plan_hashes,
)


def test_completion_still_valid_tracks_task_hash(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)
    run_init(repo, minimal=True)
    config = load_config_from_repo(repo)
    task_hash, plan_hash = task_and_plan_hashes(repo, config)
    head = head_commit(repo)
    record = CompletionRecord(
        completed_at=datetime.now(timezone.utc),
        lifecycle_id="lc-1",
        turn=1,
        worker_session_id="w",
        reviewer_session_id="r",
        initial_base_commit=head,
        final_commit=head,
        last_approved_commit=head,
        final_review_file=".auto-loop/reviews/0001-final.md",
        task_sha256=task_hash,
        plan_sha256=plan_hash,
    )
    save_completion_record(repo, record)
    assert completion_still_valid(repo, config, record)
    (repo / ".auto-loop/task.md").write_text("changed\n", encoding="utf-8")
    assert not completion_still_valid(repo, config, record)
