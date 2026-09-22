"""Controller-owned retry must respect supervision failure metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.lifecycle import LifecycleStatus, create_lifecycle
from auto_loop.loop import LifecycleRunner
from auto_loop.providers.cursor import SessionError
from auto_loop.providers.supervision import (
    ProviderAttemptResult,
    ProviderError,
    ProviderFailureKind,
    provider_attempt_from_process_output,
)
from auto_loop.run_options import RunOptions
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state

from tests.integration.scenario_harness import make_repo


@dataclass
class SequenceProvider:
    attempts: list[ProviderAttemptResult] = field(default_factory=list)

    def prepare(self, role: str) -> None:
        return None

    def invoke(self, argv: list[str]) -> ProviderAttemptResult:
        if not self.attempts:
            raise RuntimeError("no scripted provider attempts remaining")
        return self.attempts.pop(0)


def _result_line(text: str, session_id: str = "sess-1") -> str:
    return json.dumps({"type": "result", "session_id": session_id, "result": text})


def _runner(repo: Path, provider: SequenceProvider) -> LifecycleRunner:
    config = default_config()
    config.run.provider_retries = 2
    return LifecycleRunner(
        repo,
        config,
        RunOptions("auto", "auto", max_turns=3, max_runtime_minutes=60, verbose=False, quiet=True),
        provider,
    )


def test_nonzero_exit_with_terminal_result_is_not_accepted(tmp_path: Path):
    repo = make_repo(tmp_path)
    block = "<AUTO_LOOP_RESULT>\n{}\n</AUTO_LOOP_RESULT>".format(
        json.dumps({"schema_version": 2, "actor": "planner", "status": "review_requested"})
    )
    bad = provider_attempt_from_process_output(
        [_result_line(block, "sess-1")],
        1,
        expected_session_id=None,
    )
    provider = SequenceProvider([bad, bad, bad])
    runner = _runner(repo, provider)
    state = create_lifecycle(__import__("auto_loop.git", fromlist=["head_commit"]).head_commit(repo))
    save_lifecycle_state(repo, state)
    with pytest.raises(ProviderError):
        runner._invoke_slot("planner", "go", state)


def test_timeout_failure_does_not_accept_terminal_result(tmp_path: Path):
    repo = make_repo(tmp_path)
    timed_out = ProviderAttemptResult(
        lines=[
            json.dumps({"type": "system", "session_id": "sess-t"}),
            _result_line("should-not-count", "sess-t"),
        ],
        exit_code=None,
        failure=ProviderFailureKind.IDLE_TIMEOUT,
        parsed=None,
    )
    provider = SequenceProvider([timed_out, timed_out, timed_out])
    runner = _runner(repo, provider)
    state = create_lifecycle(__import__("auto_loop.git", fromlist=["head_commit"]).head_commit(repo))
    save_lifecycle_state(repo, state)
    with pytest.raises(ProviderError):
        runner._invoke_slot("planner", "go", state)


def test_malformed_stream_retries_then_fails(tmp_path: Path):
    repo = make_repo(tmp_path)
    provider = SequenceProvider(
        [
            ProviderAttemptResult(
                lines=["{not-json"],
                exit_code=0,
                failure=ProviderFailureKind.TRUNCATED,
            ),
            ProviderAttemptResult(
                lines=["{still-bad"],
                exit_code=0,
                failure=ProviderFailureKind.TRUNCATED,
            ),
            ProviderAttemptResult(
                lines=["{still-bad"],
                exit_code=0,
                failure=ProviderFailureKind.TRUNCATED,
            ),
        ]
    )
    runner = _runner(repo, provider)
    state = create_lifecycle(__import__("auto_loop.git", fromlist=["head_commit"]).head_commit(repo))
    save_lifecycle_state(repo, state)
    with pytest.raises(ProviderError, match="failed after"):
        runner._invoke_slot("planner", "go", state)


def test_interrupted_attempt_still_persists_session_id(tmp_path: Path):
    repo = make_repo(tmp_path)
    from auto_loop.git import head_commit

    interrupted = ProviderAttemptResult(
        lines=[json.dumps({"type": "system", "session_id": "persist-on-stop"})],
        exit_code=None,
        failure=ProviderFailureKind.INTERRUPTED,
        parsed=None,
    )
    provider = SequenceProvider([interrupted])
    runner = _runner(repo, provider)
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    runner._stop.requested = True
    with pytest.raises(ProviderError):
        runner._invoke_slot("planner", "go", state)
    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    assert loaded.sessions["planner"].session_id == "persist-on-stop"
    assert loaded.status == LifecycleStatus.STOPPED

    loaded.status = LifecycleStatus.RUNNING
    save_lifecycle_state(repo, loaded)
    from auto_loop.providers.scripted import ScriptedProvider

    provider2 = ScriptedProvider()
    provider2.engine.register_role_session("planner", "persist-on-stop")
    provider2.set_planner_review_request()
    runner2 = _runner(repo, provider2)
    state2 = load_lifecycle_state(repo)
    assert state2 is not None
    runner2._invoke_slot("planner", "go", state2)
    planner_calls = [inv for inv in provider2.engine.invocations if inv.role == "planner"]
    assert planner_calls[-1].resume_session_id == "persist-on-stop"


def test_interrupted_provider_is_not_retried_and_keeps_inflight(tmp_path: Path):
    repo = make_repo(tmp_path)
    from auto_loop.git import head_commit

    calls = {"n": 0}

    class Once:
        def prepare(self, role: str) -> None:
            return None

        def invoke(self, argv: list[str]) -> ProviderAttemptResult:
            del argv
            calls["n"] += 1
            return ProviderAttemptResult(
                lines=[json.dumps({"type": "thinking", "text": "partial"})],
                failure=ProviderFailureKind.INTERRUPTED,
            )

    runner = _runner(repo, Once())  # type: ignore[arg-type]
    state = create_lifecycle(head_commit(repo))
    save_lifecycle_state(repo, state)
    runner._stop.requested = True
    with pytest.raises(ProviderError):
        runner._invoke_slot("planner", "go", state)
    assert calls["n"] == 1
    loaded = load_lifecycle_state(repo)
    assert loaded is not None
    assert loaded.status == LifecycleStatus.STOPPED
    assert loaded.inflight is not None
    assert loaded.inflight.session_slot == "planner"


def test_resumed_session_mismatch_raises_session_error(tmp_path: Path):
    repo = make_repo(tmp_path)
    state = create_lifecycle(__import__("auto_loop.git", fromlist=["head_commit"]).head_commit(repo))
    state.sessions["planner"].session_id = "stored-sess"
    save_lifecycle_state(repo, state)
    provider = SequenceProvider(
        [
            ProviderAttemptResult(
                lines=[json.dumps({"type": "system", "session_id": "other-sess"})],
                exit_code=0,
                failure=ProviderFailureKind.MISSING_RESULT,
            )
        ]
    )
    runner = _runner(repo, provider)
    with pytest.raises(SessionError):
        runner._invoke_slot("planner", "go", state)
