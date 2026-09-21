"""Fake-provider lifecycle flows: revise, dirty batch, and protocol errors."""

import os
import subprocess
from pathlib import Path


from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state
from auto_loop.run_options import RunOptions


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


def _approve_plan(repo: Path, provider: ScriptedProvider) -> None:
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )


class DirtyAwareProvider:
    """Clears a dirty marker file before the second worker invocation."""

    def __init__(self, repo: Path, dirty_rel: str = "dirty.txt") -> None:
        self.repo = repo
        self.dirty_rel = dirty_rel
        self._inner = ScriptedProvider()
        self._worker_invocations = 0

    def prepare(self, role: str) -> None:
        if role == "worker":
            self._worker_invocations += 1
            if self._worker_invocations >= 2:
                dirty = self.repo / self.dirty_rel
                if dirty.is_file():
                    dirty.unlink()
        self._inner.prepare(role)

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        return self._inner.invoke(argv)

    def set_response(self, role: str, payload: dict) -> None:
        self._inner.set_response(role, payload)

    def set_worker_plan_request(self) -> None:
        self._inner.set_worker_plan_request()

    def set_reviewer_pass(self, scope: str, target: str) -> None:
        self._inner.set_reviewer_pass(scope, target)

    def set_reviewer_revise(self, scope: str, target: str, finding_id: str = "f-1") -> None:
        self._inner.set_reviewer_revise(scope, target, finding_id)


def _batch_worker_payload(base: str, head: str) -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "batch",
            "target": "W01",
            "summary": "batch",
            "base_commit": base,
            "head_commit": head,
        },
        "work_summary": "implemented",
        "verification": [],
        "notes": [],
    }


def test_plan_revise_keeps_planner_session_and_unlocks_after_pass(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_revise("plan", "plan")
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=4, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state is not None
    assert state.plan_approved is True
    assert state.sessions["planner"].session_id is not None
    assert state.sessions["worker"].session_id is None
    reviews = sorted((repo / ".ai/auto-loop" / "reviews").glob("*.md"))
    assert len(reviews) == 2


def test_batch_revise_does_not_advance_baseline(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    head = head_commit(repo)
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == baseline


def test_dirty_batch_retries_worker_then_succeeds(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = DirtyAwareProvider(repo=repo)
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
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=3, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == head
    assert outcome.exit_code == ExitCode.LIMIT_REACHED


def test_dirty_batch_exhaustion_returns_git_protocol_error(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
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
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=4, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == baseline


def test_multi_fix_cumulative_batch_pass_advances_baseline(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature v1")
    head_v1 = head_commit(repo)
    provider.set_response("worker", _batch_worker_payload(baseline, head_v1))
    provider.set_reviewer_revise("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    (repo / "feature.txt").write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "fix v2")
    head_v2 = head_commit(repo)
    provider.set_response("worker", _batch_worker_payload(baseline, head_v2))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.last_approved_commit == head_v2


def test_history_rewrite_during_run_exits_git_protocol_error(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    head = head_commit(repo)
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    _git(repo, "reset", "--hard", baseline)
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=1, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR


class PromptCapturingProvider:
    """Records reviewer prompts from fake-agent argv."""

    def __init__(self) -> None:
        self._inner = ScriptedProvider()
        self.reviewer_prompts: list[str] = []

    def prepare(self, role: str) -> None:
        self._inner.prepare(role)

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if len(argv) > 1 and argv[0] == "fake-agent":
            prompt = argv[-1]
            if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "reviewer":
                self.reviewer_prompts.append(prompt)
        return self._inner.invoke(argv)

    def set_worker_plan_request(self) -> None:
        self._inner.set_worker_plan_request()

    def set_reviewer_pass(self, scope: str, target: str) -> None:
        self._inner.set_reviewer_pass(scope, target)

    def set_response(self, role: str, payload: dict) -> None:
        self._inner.set_response(role, payload)


def test_batch_reviewer_prompt_allows_widened_inspection(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = PromptCapturingProvider()
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
    provider.set_response("worker", _batch_worker_payload(baseline, head))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert provider.reviewer_prompts
    batch_prompt = provider.reviewer_prompts[-1]
    assert "outside the targets" in batch_prompt
    assert f"{baseline}..{head}" in batch_prompt


def test_wrong_batch_head_exits_git_protocol_error(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    _approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    (repo / "feature.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    provider.set_response(
        "worker",
        _batch_worker_payload(baseline, baseline),
    )
    outcome = run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert outcome.exit_code == ExitCode.GIT_PROTOCOL_ERROR
