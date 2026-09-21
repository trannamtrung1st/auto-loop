"""Subprocess Cursor provider wiring."""

from auto_loop.config import default_config
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.providers.supervision import SupervisionOutcome


def test_subprocess_provider_delegates_to_supervision(monkeypatch):
    config = default_config()
    provider = SubprocessCursorProvider(config)
    captured: list[list[str]] = []

    def fake_stream(argv, **kwargs):
        captured.append(list(argv))
        return SupervisionOutcome(lines=['{"type":"result","session_id":"s1","result":"ok"}'], exit_code=0)

    monkeypatch.setattr(
        "auto_loop.providers.subprocess_cursor.run_subprocess_streaming",
        fake_stream,
    )
    attempt = provider.invoke(["agent", "-p", "--workspace", "/tmp", "hi"])
    assert attempt.failure is None
    assert attempt.exit_code == 0
    assert captured
    assert provider.uses_live_cursor is True
