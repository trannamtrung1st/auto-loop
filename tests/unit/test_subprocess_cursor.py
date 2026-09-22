"""Subprocess Cursor provider wiring."""

from auto_loop.config import default_config
from auto_loop.providers.subprocess_cursor import SubprocessCursorProvider
from auto_loop.providers.supervision import SupervisionOutcome


def test_subprocess_provider_delegates_to_supervision(monkeypatch):
    config = default_config()
    provider = SubprocessCursorProvider(config)
    seen: list[str] = []
    provider.on_stream_line = seen.append
    captured: list[list[str]] = []
    forwarded: list[object] = []

    def fake_stream(argv, **kwargs):
        captured.append(list(argv))
        forwarded.append(kwargs.get("on_line"))
        return SupervisionOutcome(lines=['{"type":"result","session_id":"s1","result":"ok"}'], exit_code=0)

    monkeypatch.setattr(
        "auto_loop.providers.subprocess_cursor.run_subprocess_streaming",
        fake_stream,
    )
    attempt = provider.invoke(["agent", "-p", "--workspace", "/tmp", "hi"])
    assert attempt.failure is None
    assert attempt.exit_code == 0
    assert captured
    assert forwarded == [seen.append]
    assert provider.uses_live_cursor is True
