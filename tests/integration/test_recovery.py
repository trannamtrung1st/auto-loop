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
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") in ("worker", "planner"):
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
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id="planner-session-1",
        started_at=utc_now(),
        head_before=head_commit(repo),
    )
    state.sessions["planner"].session_id = "planner-session-1"
    state.next_session = "plan_reviewer"
    save_lifecycle_state(repo, state)

    provider = PromptCapturingProvider()
    provider._inner.engine.sessions["planner"] = "planner-session-1"
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


def test_inflight_before_provider_launch_invokes_once_without_extra_commits(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    initial_head = head_commit(repo)
    state = load_lifecycle_state(repo)
    if state is None:
        from auto_loop.lifecycle import create_lifecycle

        state = create_lifecycle(initial_head)
        save_lifecycle_state(repo, state)
    state.inflight = InflightMarker(
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id="planner-session-1",
        started_at=utc_now(),
        head_before=initial_head,
    )
    state.sessions["planner"].session_id = "planner-session-1"
    state.next_session = "plan_reviewer"
    save_lifecycle_state(repo, state)

    invoke_count = 0
    provider = PromptCapturingProvider()
    provider._inner.engine.sessions["planner"] = "planner-session-1"

    def counting_invoke(argv):
        nonlocal invoke_count
        invoke_count += 1
        return provider._inner.invoke(argv)

    provider.invoke = counting_invoke  # type: ignore[method-assign]
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert invoke_count == 2
    assert head_commit(repo) == initial_head


def test_inflight_resume_with_uncommitted_product_file_keeps_single_head(tmp_path: Path):
    repo = _repo(tmp_path)
    from auto_loop.git import head_commit

    initial_head = head_commit(repo)
    partial = repo / "partial-work.txt"
    partial.write_text("in progress\n", encoding="utf-8")
    state = load_lifecycle_state(repo)
    if state is None:
        from auto_loop.lifecycle import create_lifecycle

        state = create_lifecycle(initial_head)
        save_lifecycle_state(repo, state)
    state.inflight = InflightMarker(
        session_slot="planner",
        role="planner",
        turn=state.turn,
        session_id="planner-session-1",
        started_at=utc_now(),
        head_before=initial_head,
    )
    state.sessions["planner"].session_id = "planner-session-1"
    save_lifecycle_state(repo, state)

    provider = PromptCapturingProvider()
    provider._inner.engine.sessions["planner"] = "planner-session-1"
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(
        repo,
        RunOptions("auto", "auto", max_turns=2, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )
    assert partial.read_text(encoding="utf-8") == "in progress\n"
    assert head_commit(repo) == initial_head
    assert provider.worker_prompts
    assert "interrupted" in provider.worker_prompts[0].lower()


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
