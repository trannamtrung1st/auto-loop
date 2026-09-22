"""Git is optional review evidence: dirty planning, path targets, and no repository."""

from __future__ import annotations

import os
from pathlib import Path

from auto_loop.doctor import Severity, run_doctor
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.git_policy import git_policy_issues
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.manifest import load_run_manifest
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state
from tests.integration.scenario_harness import PromptCapturingProvider, git, run_lifecycle, run_opts
from tests.repo_utils import git_repo


def _approve_plan(repo: Path, provider: ScriptedProvider) -> None:
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True


def _path_batch(path: str, target_id: str) -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "batch",
            "target": "batch",
            "summary": "review paths",
            "targets": [
                {
                    "kind": "path",
                    "id": target_id,
                    "path": path,
                    "purpose": "explicit review target",
                }
            ],
        },
        "work_summary": "wrote target",
        "verification": [],
        "notes": [],
    }


def _pass(scope: str, target: str, ids: list[str]) -> dict:
    return {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": scope,
        "target": target,
        "reviewed_target_ids": ids,
        "summary": "ok",
        "findings": [],
        "verification": [],
    }


def _dirty_product_tree(repo: Path) -> None:
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    (repo / ".gitignore").write_text("local/\n", encoding="utf-8")
    (repo / "old.txt").write_text("old\n", encoding="utf-8")
    git(repo, "add", "README.md", ".gitignore", "old.txt")
    git(repo, "commit", "-m", "baseline files")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / ".gitignore").write_text("local/\nextra/\n", encoding="utf-8")
    (repo / "old.txt").unlink()
    output = repo / "proposal"
    output.mkdir()
    (output / "output.md").write_text("local output\n", encoding="utf-8")


def test_optional_dirty_tree_allows_plan_review(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    _dirty_product_tree(repo)
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "plan_reviewer"
    assert state.active_review is not None
    assert state.active_review.scope == "plan"
    assert state.inflight is None
    assert state.completed_provider_turn is None
    _approve_plan_continue = provider
    _approve_plan_continue.set_plan_reviewer_pass()
    passed = run_lifecycle(repo, run_opts(1), provider)
    assert passed.exit_code == ExitCode.LIMIT_REACHED
    assert load_lifecycle_state(repo).plan_approved is True


def test_required_dirty_planning_is_rejected(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="required")
    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.inflight is None
    assert state.next_session == "planner"
    assert state.completed_provider_turn is not None
    assert state.completed_provider_turn.transition_error
    assert state.completed_provider_turn.result["status"] == "review_requested"
    calls = len(provider.engine.invocations)
    resumed = run_lifecycle(repo, run_opts(1), provider)
    assert resumed.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    assert len(provider.engine.invocations) == calls
    assert load_lifecycle_state(repo).inflight is None


def test_ignored_path_review_does_not_require_a_commit(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore generated")
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    generated = repo / "generated"
    generated.mkdir()
    (generated / "result.md").write_text("ok\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("generated/result.md", "result"))
    provider.set_response("reviewer", _pass("batch", "batch", ["result"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == baseline
    assert head_commit(repo) == baseline
    assert any(item.kind == "path" and item.id == "result" for item in state.approved_evidence)
    reviews = sorted((repo / ".ai" / "auto-loop" / "reviews").glob("*.md"))
    assert "generated/result.md" in reviews[-1].read_text(encoding="utf-8")


def test_untracked_and_tracked_dirty_paths_are_reviewable(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "tracked")
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "report.md").write_text("report\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("report.md", "report"))
    provider.set_response("reviewer", _pass("batch", "batch", ["report"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    assert load_lifecycle_state(repo).last_approved_commit == baseline

    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("tracked.txt", "tracked"))
    provider.set_response("reviewer", _pass("batch", "batch", ["tracked"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == baseline
    assert any(item.id == "tracked" and item.fingerprint for item in state.approved_evidence)


def test_git_range_and_path_target_pass_advances_baseline(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    head = head_commit(repo)
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature")
    feature = head_commit(repo)
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    payload = _path_batch("notes.md", "notes")
    provider.set_response("worker", payload)
    provider.set_response("reviewer", _pass("batch", "batch", ["git", "notes"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == feature
    assert state.last_approved_commit != head


def test_required_mode_still_rejects_untracked_non_ignored_paths(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="required")
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    (repo / "report.md").write_text("report\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("report.md", "report"))
    provider.set_response("worker", _path_batch("report.md", "report"))
    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


class _MutatingReviewer(ScriptedProvider):
    def __init__(self, repo: Path, rel: str) -> None:
        super().__init__()
        self.repo = repo
        self.rel = rel

    def invoke(self, argv: list[str]):
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "reviewer":
            (self.repo / self.rel).write_text("mutated\n", encoding="utf-8")
        return super().invoke(argv)


def test_reviewer_path_mutation_is_detected_without_a_clean_tree(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    provider = _MutatingReviewer(repo, "report.md")
    _approve_plan(repo, provider)
    (repo / "report.md").write_text("report\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("report.md", "report"))
    provider.set_response("reviewer", _pass("batch", "batch", ["report"]))
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.REVIEW_MUTATION_ERROR


def _final_path_request(path: str, target_id: str) -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "final",
            "target": "whole-task",
            "summary": "ready for final review",
            "targets": [
                {
                    "kind": "path",
                    "id": target_id,
                    "path": path,
                    "purpose": "final review evidence",
                }
            ],
        },
        "work_summary": "ready",
        "verification": [],
        "notes": [],
    }


def _queue_no_git_lifecycle(provider: ScriptedProvider, *, path: str, target_id: str) -> None:
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _path_batch(path, target_id))
    provider.set_response("reviewer", _pass("batch", "batch", [target_id]))
    provider.set_response("worker", _final_path_request(path, target_id))
    provider.set_reviewer_complete(target_ids=[target_id])


class _WritingWorker(ScriptedProvider):
    def __init__(self, repo: Path, rel: str, text: str) -> None:
        super().__init__()
        self.repo = repo
        self.rel = rel
        self.text = text

    def invoke(self, argv: list[str]):
        if os.environ.get("AUTO_LOOP_FAKE_SLOT") == "worker":
            path = self.repo / self.rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.text, encoding="utf-8")
        return super().invoke(argv)


def test_mode_off_completes_without_a_git_repository(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    bootstrap_workspace(repo, git_mode="off")
    provider = _WritingWorker(repo, "generated/result.md", "done\n")
    _queue_no_git_lifecycle(provider, path="generated/result.md", target_id="result")
    outcome = run_lifecycle(repo, run_opts(8), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status.value == "completed"
    assert state.initial_base_commit is None
    assert state.last_approved_commit is None
    assert (repo / "generated" / "result.md").is_file()
    assert (repo / ".ai" / "auto-loop" / "runtime" / "completion.json").is_file()


def test_optional_mode_completes_without_a_git_repository(tmp_path: Path):
    repo = tmp_path / "plain"
    repo.mkdir()
    bootstrap_workspace(repo, git_mode="optional")
    provider = _WritingWorker(repo, "report.md", "analysis\n")
    _queue_no_git_lifecycle(provider, path="report.md", target_id="report")
    outcome = run_lifecycle(repo, run_opts(8), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit is None


def test_final_complete_with_path_only_output(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore generated")
    provider = _WritingWorker(repo, "generated/result.md", "final\n")
    provider.set_planner_review_request()
    provider.set_plan_reviewer_pass()
    provider.set_response("worker", _path_batch("generated/result.md", "result"))
    provider.set_response("reviewer", _pass("batch", "batch", ["result"]))
    final = {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "final",
            "target": "whole-task",
            "summary": "ready",
            "targets": [
                {
                    "kind": "path",
                    "id": "result",
                    "path": "generated/result.md",
                    "purpose": "generated output",
                }
            ],
        },
        "work_summary": "ready",
        "verification": [],
        "notes": [],
    }
    provider.set_response("worker", final)
    provider.set_reviewer_complete(target_ids=["result"])
    outcome = run_lifecycle(repo, run_opts(8), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state.initial_base_commit == head_commit(repo)
    assert state.last_approved_commit == state.initial_base_commit
    assert any(item.id == "result" and item.kind == "path" for item in state.approved_evidence)


class _MutatingPlanner(ScriptedProvider):
    def __init__(self, repo: Path, rel: str, text: str) -> None:
        super().__init__()
        self.repo = repo
        self.rel = rel
        self.text = text

    def invoke(self, argv: list[str]):
        if os.environ.get("AUTO_LOOP_FAKE_SLOT") == "planner":
            (self.repo / self.rel).write_text(self.text, encoding="utf-8")
        return super().invoke(argv)


def test_optional_rejects_planner_mutating_already_dirty_file(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    (repo / "README.md").write_text("already dirty\n", encoding="utf-8")
    provider = _MutatingPlanner(repo, "README.md", "planner edited\n")
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "planner"
    assert state.completed_provider_turn is not None
    assert "mutated" in (state.completed_provider_turn.transition_error or "").lower()


def test_optional_rejects_planner_mutating_product_in_unborn_repo(tmp_path: Path):
    repo = tmp_path / "unborn-plan"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    bootstrap_workspace(repo, git_mode="optional", commit_git_inputs=False)
    (repo / ".gitignore").write_text("local/\n", encoding="utf-8")
    provider = _MutatingPlanner(repo, ".gitignore", "planner edited\n")
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.next_session == "planner"
    assert state.completed_provider_turn is not None
    assert "mutated" in (state.completed_provider_turn.transition_error or "").lower()


def test_optional_final_without_targets_is_rejected(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    (repo / "report.md").write_text("v1\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("report.md", "report"))
    provider.set_response("reviewer", _pass("batch", "batch", ["report"]))
    (repo / "report.md").write_text("v2\n", encoding="utf-8")
    provider.set_worker_final_request()
    provider.set_reviewer_complete(target_ids=[])
    outcome = run_lifecycle(repo, run_opts(3), provider)
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.status.value != "completed"
    assert state.inflight is not None
    assert state.completed_provider_turn is None
    assert state.next_session == "worker"
    assert state.inflight.repair_reason
    assert "reviewable evidence" in state.inflight.repair_reason.lower()


def test_optional_final_without_targets_worker_repairs_on_resume(tmp_path: Path):
    repo = git_repo(tmp_path)
    bootstrap_workspace(repo, git_mode="optional")
    provider = PromptCapturingProvider()
    _approve_plan(repo, provider)
    (repo / "report.md").write_text("v1\n", encoding="utf-8")
    provider.set_response("worker", _path_batch("report.md", "report"))
    provider.set_response("reviewer", _pass("batch", "batch", ["report"]))
    (repo / "report.md").write_text("v2\n", encoding="utf-8")
    provider.set_worker_final_request()
    rejected = run_lifecycle(repo, run_opts(3), provider)
    assert rejected.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state is not None
    worker_session_before = state.sessions["worker"].session_id
    provider.set_response("worker", _final_path_request("report.md", "report"))
    provider.set_reviewer_complete(target_ids=["report"])
    prompts_before_resume = len(provider.worker_prompts)
    completed = run_lifecycle(repo, run_opts(3), provider)
    assert len(provider.worker_prompts) > prompts_before_resume
    repair_prompt = provider.worker_prompts[-1]
    assert "controller rejected it" in repair_prompt.lower()
    assert "reviewable evidence" in repair_prompt.lower()
    assert "interrupted" not in repair_prompt.lower()
    assert completed.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.sessions["worker"].session_id == worker_session_before


def test_optional_unborn_git_repository_completes_with_path_targets(tmp_path: Path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    bootstrap_workspace(repo, git_mode="optional", commit_git_inputs=False)
    provider = _WritingWorker(repo, "report.md", "analysis\n")
    _queue_no_git_lifecycle(provider, path="report.md", target_id="report")
    outcome = run_lifecycle(repo, run_opts(8), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.initial_base_commit is None
    assert state.last_approved_commit is None


def test_doctor_matches_git_policy(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("auto_loop.doctor.resolve_cursor_binary", lambda _cfg: "/usr/bin/fake-agent")

    missing = tmp_path / "missing-git"
    missing.mkdir()
    bootstrap_workspace(missing, git_mode="required")
    required_issues = git_policy_issues(missing, load_run_manifest(missing / ".ai" / "run.yaml").config)
    assert any(issue.severity == "error" for issue in required_issues)
    report = run_doctor(load_run_manifest(missing / ".ai" / "run.yaml"))
    assert any(check.severity == Severity.ERROR and check.check_id == "git" for check in report.checks)
    outcome = run_lifecycle(missing, run_opts(1), ScriptedProvider())
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR

    optional = git_repo(tmp_path, "optional-dirty")
    bootstrap_workspace(optional, git_mode="optional")
    (optional / "README.md").write_text("dirty\n", encoding="utf-8")
    source = load_run_manifest(optional / ".ai" / "run.yaml")
    optional_issues = git_policy_issues(optional, source.config)
    assert any(issue.severity == "warning" for issue in optional_issues)
    assert not any(issue.severity == "error" for issue in optional_issues)
    optional_report = run_doctor(source)
    assert optional_report.ok
    assert any(check.severity == Severity.WARNING and check.check_id == "git" for check in optional_report.checks)

    plain = tmp_path / "off"
    plain.mkdir()
    bootstrap_workspace(plain, git_mode="off")
    off_issues = git_policy_issues(plain, load_run_manifest(plain / ".ai" / "run.yaml").config)
    assert all(issue.severity == "ok" for issue in off_issues)
    off_report = run_doctor(load_run_manifest(plain / ".ai" / "run.yaml"))
    assert not any(
        check.severity == Severity.ERROR and check.check_id == "git" for check in off_report.checks
    )
