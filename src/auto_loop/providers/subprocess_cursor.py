"""Live Cursor CLI subprocess provider."""

from __future__ import annotations

import re
from dataclasses import dataclass

from auto_loop.config import AutoLoopConfig
from auto_loop.providers.supervision import (
    PidCallback,
    StopCheck,
    run_subprocess_streaming,
)

_RESUME_RE = re.compile(r"--resume=([^\s]+)")


def _resume_session_id(argv: list[str]) -> str | None:
    for arg in argv:
        match = _RESUME_RE.match(arg)
        if match:
            return match.group(1)
    return None


@dataclass
class SubprocessCursorProvider:
    """Invoke the real Cursor agent CLI as a supervised subprocess.

    Performs a single supervised attempt per ``invoke`` call. The lifecycle
    controller owns retry policy and rebuilds ``--resume`` after adopting any
    session id observed in a truncated or incomplete stream.
    """

    config: AutoLoopConfig
    uses_live_cursor: bool = True
    stop_check: StopCheck | None = None
    on_provider_pid: PidCallback | None = None

    def prepare(self, role: str) -> None:
        return None

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]:
        limits = self.config.limits
        expected_session_id = _resume_session_id(argv)
        outcome = run_subprocess_streaming(
            argv,
            wall_timeout_seconds=float(limits.agent_timeout_seconds),
            idle_timeout_seconds=float(limits.agent_idle_timeout_seconds),
            expected_session_id=expected_session_id,
            stop_check=self.stop_check,
            on_provider_pid=self.on_provider_pid,
        )
        return outcome.exit_code or 0, list(outcome.lines)
