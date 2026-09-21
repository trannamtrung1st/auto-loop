"""Final review, completion, blocker adjudication, and post-completion run behavior."""

import json
import subprocess
from pathlib import Path

import pytest

from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.run_prerequisites import RunPreconditionError
from auto_loop.runtime import load_lifecycle_state
from auto_loop.terminal_records import completion_path, load_completion_record


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    bootstrap_workspace(repo)
    return repo


def _approve_plan_and_batch(repo: Path, provider: ScriptedProvider) -> str:
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    head = head_commit(repo)
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "W01",
                "summary": "batch",
                "base_commit": baseline,
                "head_commit": head,
            },
            "work_summary": "implemented",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    return head_commit(repo)


def test_final_complete_writes_completion_and_exits_zero(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    head = _approve_plan_and_batch(repo, provider)
    provider.set_worker_final_request(head=head)
    provider.set_reviewer_complete(head)
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.COMPLETE
    record = load_completion_record(repo)
    assert record is not None
    assert record.final_commit == head
    assert completion_path(repo).is_file()
    assert record.planner_session_id and record.plan_reviewer_session_id
    assert record.worker_session_id and record.reviewer_session_id
    session_ids = {
        record.planner_session_id,
        record.plan_reviewer_session_id,
        record.worker_session_id,
        record.reviewer_session_id,
    }
    assert len(session_ids) == 4
    assert record.initial_approved_plan_sha256


def test_final_request_with_unreviewed_head_stays_on_worker(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    _approve_plan_and_batch(repo, provider)
    (repo / "extra.txt").write_text("unreviewed\n", encoding="utf-8")
    _git(repo, "add", "extra.txt")
    _git(repo, "commit", "-m", "extra")
    provider.set_worker_final_request()
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.next_actor == "worker"
    assert state.active_review is None
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_final_revise_requires_batch_before_repeat(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    approved = _approve_plan_and_batch(repo, provider)
    provider.set_worker_final_request(head=approved)
    provider.set_reviewer_revise("final", "whole-task")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-m", "fix")
    head = head_commit(repo)
    provider.set_response(
        "worker",
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "W02",
                "summary": "fix batch",
                "base_commit": approved,
                "head_commit": head,
            },
            "work_summary": "fixed",
            "verification": [],
            "notes": [],
        },
    )
    provider.set_reviewer_pass("batch", "W02")
    provider.set_worker_final_request(head=head)
    provider.set_reviewer_complete(head)
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=4, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.COMPLETE
    assert load_completion_record(repo).final_commit == head


def test_worker_blocked_reviewer_revise_resumes_worker(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    provider.set_worker_blocked()
    provider.set_reviewer_revise("batch", "blocked")
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.next_actor == "worker"
    assert not (repo / ".auto-loop/runtime/blocked.json").is_file()
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_worker_blocked_reviewer_blocked_writes_record(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    provider.set_worker_blocked()
    provider.set_reviewer_blocked()
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.BLOCKED
    blocked = json.loads((repo / ".auto-loop/runtime/blocked.json").read_text(encoding="utf-8"))
    assert blocked["status"] == "blocked"


def test_completed_lifecycle_rerun_is_idempotent(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    head = _approve_plan_and_batch(repo, provider)
    provider.set_worker_final_request(head=head)
    provider.set_reviewer_complete(head)
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        ScriptedProvider(),
    )
    assert outcome.exit_code == ExitCode.COMPLETE
    assert outcome.message is not None


def test_completed_lifecycle_rejects_changed_task(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    head = _approve_plan_and_batch(repo, provider)
    provider.set_worker_final_request(head=head)
    provider.set_reviewer_complete(head)
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    (repo / ".auto-loop/task.md").write_text("changed task\n", encoding="utf-8")
    with pytest.raises(RunPreconditionError):
        run_lifecycle(
            repo,
            RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
            ScriptedProvider(),
        )
