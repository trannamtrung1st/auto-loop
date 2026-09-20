"""Session identity validation tests."""

import json

import pytest

from auto_loop.providers.cursor import SessionError, assert_resume_session_matches, parse_cursor_stream


def test_assert_resume_session_matches_accepts_equal():
    assert_resume_session_matches("id-1", "id-1")


def test_assert_resume_session_matches_rejects_different():
    with pytest.raises(SessionError):
        assert_resume_session_matches("id-1", "id-2")


def test_unknown_stream_fields_ignored():
    lines = [
        json.dumps(
            {
                "type": "system",
                "session_id": "sess",
                "future_field": {"nested": True},
            }
        ),
        json.dumps({"type": "result", "session_id": "sess", "result": "ok", "extra": 1}),
    ]
    parsed = parse_cursor_stream(lines)
    assert parsed.session_id == "sess"
    assert parsed.final_text == "ok"
