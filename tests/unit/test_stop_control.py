"""Stop controller behavior."""

import signal
from pathlib import Path

from auto_loop.stop_control import RunStopController


def test_stop_signal_terminates_active_provider_pid(monkeypatch):
    terminated: list[int] = []

    def fake_terminate(pid: int, **kwargs: object) -> None:
        terminated.append(pid)

    monkeypatch.setattr("auto_loop.stop_control.terminate_process_tree", fake_terminate)
    ctrl = RunStopController(repo=Path("/tmp/repo"))
    ctrl.set_active_provider(4242)
    ctrl._handle(signal.SIGINT, None)
    assert ctrl.requested is True
    assert terminated == [4242]
