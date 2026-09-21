"""Cursor command construction and stream-json parsing tests."""

import json
from pathlib import Path

import pytest

from auto_loop.config import default_config
from auto_loop.providers.base import AgentRequest
from auto_loop.providers.cursor import (
    SessionError,
    StreamParseError,
    assert_distinct_session_ids,
    build_cursor_command,
    parse_cursor_stream,
    role_extra_args,
)


def _request(role: str = "worker") -> AgentRequest:
    session_purpose = role if role in ("planner", "plan_reviewer", "worker", "reviewer") else "worker"
    logical = "reviewer" if role in ("reviewer", "plan_reviewer") else role
    return AgentRequest(
        role=logical,  # type: ignore[arg-type]
        session_purpose=session_purpose,  # type: ignore[arg-type]
        workspace=Path("/tmp/workspace"),
        prompt="do work",
        model="auto",
        mode="agent" if logical != "reviewer" else "ask",
        timeout_seconds=60,
        idle_timeout_seconds=30,
    )


def test_build_create_command_includes_stream_json_and_workspace():
    config = default_config()
    argv = build_cursor_command(config, _request("worker"), binary="/usr/bin/agent")
    assert argv[0] == "/usr/bin/agent"
    assert "-p" in argv
    assert "--trust" in argv
    assert "--output-format" in argv
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--resume=" not in " ".join(argv)
    assert "--force" in argv


def test_build_resume_command_uses_explicit_session_id():
    config = default_config()
    argv = build_cursor_command(
        config,
        _request("reviewer"),
        binary="/usr/bin/agent",
        resume_session_id="sess-123",
    )
    assert "--resume=sess-123" in argv
    assert "--mode=ask" in argv


def test_reviewer_extra_args_forwarded():
    config = default_config()
    config.provider.cursor.reviewer_extra_args = ["--foo"]
    assert role_extra_args(config, "reviewer") == ["--foo"]


def test_parse_stream_captures_session_and_result():
    lines = [
        json.dumps({"type": "system", "session_id": "abc-123", "request_id": "req-1"}),
        json.dumps({"type": "assistant", "message": {"content": "partial "}}),
        json.dumps({"type": "result", "session_id": "abc-123", "result": "partial done"}),
    ]
    parsed = parse_cursor_stream(lines)
    assert parsed.session_id == "abc-123"
    assert parsed.request_id == "req-1"
    assert parsed.final_text == "partial done"


def test_resume_session_mismatch_fails():
    lines = [json.dumps({"type": "system", "session_id": "other"})]
    with pytest.raises(SessionError):
        parse_cursor_stream(lines, expected_session_id="expected-id")


def test_malformed_line_raises():
    with pytest.raises(StreamParseError):
        parse_cursor_stream(["not-json"])


def test_distinct_session_ids_enforced():
    assert_distinct_session_ids("a", "b")
    with pytest.raises(SessionError):
        assert_distinct_session_ids("same", "same")
