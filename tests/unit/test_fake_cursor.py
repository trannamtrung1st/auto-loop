"""Fake Cursor provider behavior tests."""

import json

import pytest

from auto_loop.providers.cursor import SessionError, parse_cursor_stream
from auto_loop.providers.fake_cursor import (
    ExpectedInvocation,
    FakeBehavior,
    FakeCursorEngine,
    FakeCursorError,
)


def _argv(prompt: str = "hello", resume: str | None = None, mode: str | None = None) -> list[str]:
    args = [
        "fake-agent",
        "-p",
        "--workspace",
        "/tmp/ws",
        "--model",
        "auto",
        "--output-format",
        "stream-json",
    ]
    if mode:
        args.append(f"--mode={mode}")
    if resume:
        args.append(f"--resume={resume}")
    args.append(prompt)
    return args


def test_new_session_emits_session_id(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "worker")
    engine = FakeCursorEngine(strict=False)
    code, lines = engine.run(_argv("plan turn"))
    assert code == 0
    parsed = parse_cursor_stream(lines)
    assert parsed.session_id is not None
    assert parsed.final_text


def test_resume_requires_exact_session_id(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "worker")
    engine = FakeCursorEngine(strict=False)
    code1, lines1 = engine.run(_argv("first"))
    session_id = parse_cursor_stream(lines1).session_id
    assert session_id
    code2, lines2 = engine.run(_argv("second", resume=session_id))
    assert code2 == 0
    with pytest.raises(FakeCursorError):
        engine.run(_argv("bad resume", resume="not-the-session"))


def test_strict_expectation_enforced(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "reviewer")
    engine = FakeCursorEngine()
    engine.expected = ExpectedInvocation(role="worker", mode="ask")
    with pytest.raises(FakeCursorError, match="unexpected role"):
        engine.run(_argv(mode="ask"))


def test_malformed_stream_mode():
    engine = FakeCursorEngine(strict=False)
    engine.behavior = FakeBehavior.MALFORMED
    code, lines = engine.run(_argv())
    assert code == 1
    assert "not-json" in lines[0]


def test_session_mismatch_behavior(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "worker")
    engine = FakeCursorEngine(strict=False)
    engine.behavior = FakeBehavior.SESSION_MISMATCH
    code, lines = engine.run(_argv())
    assert code == 0
    with pytest.raises(SessionError):
        parse_cursor_stream(lines, expected_session_id=engine.sessions["worker"])


def test_invocation_transcript_captured(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "worker")
    engine = FakeCursorEngine(strict=False)
    engine.run(_argv("batch prompt"))
    assert len(engine.invocations) == 1
    assert engine.invocations[0].prompt == "batch prompt"


def test_tool_activity_event_supported(monkeypatch):
    monkeypatch.setenv("AUTO_LOOP_FAKE_ROLE", "worker")
    engine = FakeCursorEngine(strict=False)
    engine.response_text = json.dumps(
        {"note": "tool events may appear in real streams; fake returns result text"}
    )
    code, lines = engine.run(_argv())
    assert code == 0
    assert any(json.loads(line)["type"] == "assistant" for line in lines)
