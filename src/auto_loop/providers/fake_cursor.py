"""Deterministic fake Cursor CLI for offline tests."""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FakeCursorError(Exception):
    """Unexpected fake provider invocation."""


class FakeBehavior(StrEnum):
    OK = "ok"
    CRASH = "crash"
    MALFORMED = "malformed"
    SESSION_MISMATCH = "session_mismatch"
    MISSING_RESULT = "missing_result"
    NONZERO_EXIT = "nonzero_exit"


@dataclass
class FakeInvocation:
    argv: list[str]
    workspace: str | None
    model: str | None
    mode: str | None
    resume_session_id: str | None
    prompt: str
    role: str | None = None


@dataclass
class ExpectedInvocation:
    role: str | None = None
    mode: str | None = None
    resume_session_id: str | None = None
    prompt_substring: str | None = None


@dataclass
class FakeCursorEngine:
    """Scriptable fake Cursor process with durable invocation transcripts."""

    sessions: dict[str, str] = field(default_factory=dict)
    invocations: list[FakeInvocation] = field(default_factory=list)
    behavior: FakeBehavior = FakeBehavior.OK
    response_text: str = "fake provider response"
    strict: bool = True
    expected: ExpectedInvocation | None = None
    _pending_session: str | None = None

    def register_role_session(self, role: str, session_id: str) -> None:
        self.sessions[role] = session_id

    def clear(self) -> None:
        self.sessions.clear()
        self.invocations.clear()
        self.expected = None
        self.behavior = FakeBehavior.OK

    def parse_argv(self, argv: list[str]) -> FakeInvocation:
        if "-p" not in argv:
            raise FakeCursorError("fake cursor requires -p")
        workspace = None
        model = None
        mode = None
        resume = None
        idx = 0
        while idx < len(argv):
            arg = argv[idx]
            if arg == "--workspace" and idx + 1 < len(argv):
                workspace = argv[idx + 1]
                idx += 2
                continue
            if arg == "--model" and idx + 1 < len(argv):
                model = argv[idx + 1]
                idx += 2
                continue
            if arg.startswith("--mode="):
                mode = arg.split("=", 1)[1]
            elif arg == "--mode" and idx + 1 < len(argv):
                mode = argv[idx + 1]
                idx += 2
                continue
            if arg.startswith("--resume="):
                resume = arg.split("=", 1)[1]
            idx += 1
        prompt = argv[-1]
        role = os.environ.get("AUTO_LOOP_FAKE_ROLE") or os.environ.get("AUTO_LOOP_FAKE_SLOT")
        record = FakeInvocation(
            argv=list(argv),
            workspace=workspace,
            model=model,
            mode=mode,
            resume_session_id=resume,
            prompt=prompt,
            role=role,
        )
        return record

    def _validate_expected(self, record: FakeInvocation) -> None:
        if not self.strict or self.expected is None:
            return
        exp = self.expected
        if exp.role is not None and record.role != exp.role:
            raise FakeCursorError(f"unexpected role {record.role!r}, expected {exp.role!r}")
        if exp.mode is not None and record.mode != exp.mode:
            raise FakeCursorError(f"unexpected mode {record.mode!r}, expected {exp.mode!r}")
        if exp.resume_session_id is not None and record.resume_session_id != exp.resume_session_id:
            raise FakeCursorError(
                f"unexpected resume {record.resume_session_id!r}, expected {exp.resume_session_id!r}"
            )
        if exp.prompt_substring and exp.prompt_substring not in record.prompt:
            raise FakeCursorError("unexpected prompt content for fake provider scenario")

    def _resolve_session_id(self, record: FakeInvocation) -> str:
        role = record.role or "default"
        if record.resume_session_id:
            expected = self.sessions.get(role)
            if expected is None:
                raise FakeCursorError(f"no stored session for role {role}")
            if expected != record.resume_session_id:
                raise FakeCursorError("resume session id does not match stored session")
            return expected
        session_id = str(uuid.uuid4())
        self.sessions[role] = session_id
        return session_id

    def run(self, argv: list[str]) -> tuple[int, list[str]]:
        record = self.parse_argv(argv)
        self._validate_expected(record)
        self.invocations.append(record)

        if self.behavior == FakeBehavior.CRASH:
            raise FakeCursorError("simulated provider crash")

        session_id = self._resolve_session_id(record)
        if self.behavior == FakeBehavior.SESSION_MISMATCH:
            session_id = "wrong-session-id"

        lines: list[str] = []
        if self.behavior == FakeBehavior.MALFORMED:
            lines.append("{ not-json")
            return 1, lines

        payload = self.response_text
        if self.behavior == FakeBehavior.MISSING_RESULT:
            lines.append(json.dumps({"type": "system", "session_id": session_id}))
            return 0, lines

        events: list[dict[str, Any]] = [
            {"type": "system", "subtype": "init", "session_id": session_id},
            {"type": "assistant", "message": {"content": payload}},
            {"type": "result", "session_id": session_id, "result": payload},
        ]
        lines = [json.dumps(event) for event in events]
        if self.behavior == FakeBehavior.NONZERO_EXIT:
            return 1, lines
        return 0, lines


_GLOBAL_ENGINE = FakeCursorEngine()


def engine() -> FakeCursorEngine:
    return _GLOBAL_ENGINE


def run_fake_cursor_cli(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    try:
        code, lines = _GLOBAL_ENGINE.run(args)
    except FakeCursorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return code


def main() -> None:
    raise SystemExit(run_fake_cursor_cli())
