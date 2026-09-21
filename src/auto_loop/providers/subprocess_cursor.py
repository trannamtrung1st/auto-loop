"""Live Cursor CLI subprocess provider."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from auto_loop.config import AutoLoopConfig
from auto_loop.providers.supervision import (
    PidCallback,
    StopCheck,
    run_subprocess_streaming,
    run_with_provider_retries,
)

_RESUME_RE = re.compile(r"--resume=([^\s]+)")


def _resume_session_id(argv: list[str]) -> str:
    for arg in argv:
        match = _RESUME_RE.match(arg)
        if match:
            return match.group(1)
    return "new-session"


@dataclass
class SubprocessCursorProvider:
    """Invoke the real Cursor agent CLI as a supervised subprocess."""

    config: AutoLoopConfig
    uses_live_cursor: bool = True
    stop_check: StopCheck | None = None
    on_provider_pid: PidCallback | None = None

    def prepare(self, role: str) -> None:
        return None

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        limits = self.config.limits
        session_id = _resume_session_id(argv)

        def _attempt(_index: int):
            return run_subprocess_streaming(
                argv,
                wall_timeout_seconds=float(limits.agent_timeout_seconds),
                idle_timeout_seconds=float(limits.agent_idle_timeout_seconds),
                expected_session_id=session_id if session_id != "new-session" else None,
                stop_check=self.stop_check,
                on_provider_pid=self.on_provider_pid,
            )

        result = run_with_provider_retries(
            _attempt,
            provider_retries=limits.provider_retries,
            session_id=session_id,
        )
        last = result.last
        return last.exit_code or 0, list(last.lines)
