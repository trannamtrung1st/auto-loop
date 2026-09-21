"""Inflight reconciliation and interruption recovery."""

import os
import subprocess
from pathlib import Path

from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import InflightMarker, utc_now
from auto_loop.loop import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    run_init(repo)
    return repo


class PromptCapturingProvider:
    def __init__(self) -> None:
        self._inner = ScriptedProvider()
        self.worker_prompts: list[str] = []

    def prepare(self, role: str) -> None:
        self._inner.prepare(role)

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == "worker":
            self.worker_prompts.append(argv[-1])
        return self._inner.invoke(argv)

    def set_worker_plan_request(self) -> None:
        self._inner.set_worker_plan_request()

    def set_reviewer_pass(self, scope: str, target: str) -> None:
        self._inner.set_reviewer_pass(scope, target)


def test_stale_inflight_resumes_worker_with_reconciliation_prompt(tmp_path: Path):
    repo = _repo(tmp_path)
    state = load_lifecycle_state(repo)
    if state is None:
        from auto_loop.lifecycle import create_lifecycle

        state = create_lifecycle(head_commit(repo))
    state.inflight = InflightMarker(
        actor="worker",
        turn=state.turn,
        session_id="worker-session-1",
        started_at=utc_now(),
        head_before=head_commit(repo),
    )
    state.sessions["worker"].session_id = "worker-session-1"
    state.next_actor = "reviewer"
    save_lifecycle_state(repo, state)

    provider = PromptCapturingProvider()
    provider._inner.engine.sessions["worker"] = "worker-session-1"
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert provider.worker_prompts
    assert "interrupted" in provider.worker_prompts[0].lower()
    reloaded = load_lifecycle_state(repo)
    assert reloaded.inflight is None


def test_inflight_cleared_after_successful_handoff(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    state = load_lifecycle_state(repo)
    assert state.inflight is None
