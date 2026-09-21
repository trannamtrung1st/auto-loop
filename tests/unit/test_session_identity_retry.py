"""Session id must persist across provider retries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from auto_loop.exits import ExitCode
from auto_loop.providers.argv_session import resume_session_id_from_argv
from auto_loop.providers.fake_cursor import FakeBehavior, FakeCursorEngine
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.providers.supervision import provider_attempt_from_process_output
from auto_loop.runtime import load_lifecycle_state

from tests.integration.scenario_harness import make_repo, run_lifecycle, run_opts


@dataclass
class FlakyScriptedProvider(ScriptedProvider):
    """Fail the first provider invocation without a final result, then succeed."""

    engine: FakeCursorEngine = field(default_factory=FakeCursorEngine)
    _fail_next: bool = True

    def invoke(self, argv: list[str]):
        if self._fail_next:
            self._fail_next = False
            self.engine.behavior = FakeBehavior.MISSING_RESULT
            code, lines = self.engine.run(argv)
            self.engine.behavior = FakeBehavior.OK
            return provider_attempt_from_process_output(
                lines,
                code,
                expected_session_id=resume_session_id_from_argv(argv),
            )
        return super().invoke(argv)


def _bump_provider_retries(repo: Path, retries: int = 2) -> None:
    path = repo / ".auto-loop" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["limits"]["provider_retries"] = retries
    path.write_text(yaml.dump(data), encoding="utf-8")


def test_provider_retry_resumes_same_session_id(tmp_path: Path):
    repo = make_repo(tmp_path)
    _bump_provider_retries(repo)
    provider = FlakyScriptedProvider()
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan", slot="plan_reviewer")
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.LIMIT_REACHED
    invocations = [inv for inv in provider.engine.invocations if inv.role == "planner"]
    assert len(invocations) >= 2
    assert invocations[0].resume_session_id is None
    stored = load_lifecycle_state(repo).sessions["planner"].session_id
    assert stored is not None
    assert invocations[1].resume_session_id == stored
