"""Controller-owned retry must respect supervision failure metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.exits import ExitCode
from auto_loop.init_cmd import run_init
from auto_loop.lifecycle import create_lifecycle
from auto_loop.loop import LifecycleRunner
from auto_loop.providers.cursor import SessionError
from auto_loop.providers.supervision import (
    ProviderAttemptResult,
    ProviderError,
    ProviderFailureKind,
    provider_attempt_from_process_output,
)
from auto_loop.run_options import RunOptions
from auto_loop.runtime import save_lifecycle_state

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
    config.limits.provider_retries = 2
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
