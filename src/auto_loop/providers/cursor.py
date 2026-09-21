"""Cursor CLI command construction and stream-json parsing."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from auto_loop.config import AutoLoopConfig, CursorProviderSettings
from auto_loop.exits import ExitCode
from auto_loop.providers.base import AgentRequest


class SessionError(Exception):
    """Persistent session identity violation."""

    exit_code = ExitCode.SESSION_ERROR


class StreamParseError(Exception):
    """Malformed or incomplete Cursor stream-json payload."""

    exit_code = ExitCode.PROVIDER_ERROR


class StreamIssueCode(StrEnum):
    MALFORMED_LINE = "malformed_line"
    OVERSIZED_LINE = "oversized_line"
    MISSING_SESSION = "missing_session"
    MISSING_RESULT = "missing_result"
    SESSION_MISMATCH = "session_mismatch"


@dataclass(frozen=True)
class StreamDiagnostic:
    code: StreamIssueCode
    message: str
    line_number: int | None = None


@dataclass
class CursorStreamParseResult:
    session_id: str | None = None
    request_id: str | None = None
    model: str | None = None
    final_text: str = ""
    usage: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[StreamDiagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diagnostics and self.session_id is not None and bool(self.final_text)


MAX_STREAM_LINE_BYTES = 1_048_576


def resolve_cursor_binary(settings: CursorProviderSettings) -> str:
    candidates = [settings.command, "agent", "cursor-agent"]
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        f"Cursor CLI not found (tried: {', '.join(seen)}). Install Cursor agent or set provider.cursor.command."
    )


def role_extra_args(config: AutoLoopConfig, role: str) -> list[str]:
    cursor = config.provider.cursor
    if role in ("planner",):
        return list(cursor.planner_extra_args)
    if role == "worker":
        return list(cursor.worker_extra_args)
    if role in ("reviewer", "plan_reviewer"):
        return list(cursor.reviewer_extra_args)
    raise ValueError(f"Unknown role: {role}")


def build_cursor_command(
    config: AutoLoopConfig,
    request: AgentRequest,
    *,
    binary: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build argv for a Cursor create or resume invocation."""
    executable = binary or resolve_cursor_binary(config.provider.cursor)
    argv: list[str] = [executable, "-p", "--trust"]
    if resume_session_id:
        argv.append(f"--resume={resume_session_id}")
    argv.extend(
        [
            "--workspace",
            str(request.workspace.resolve()),
            "--model",
            request.model,
            "--output-format",
            "stream-json",
        ]
    )
    if request.mode == "ask":
        argv.append("--mode=ask")
    argv.extend(role_extra_args(config, request.session_purpose))
    argv.extend(request.extra_args)
    argv.append(request.prompt)
    return argv


def assert_distinct_session_ids(worker_session_id: str | None, reviewer_session_id: str | None) -> None:
    if worker_session_id and reviewer_session_id and worker_session_id == reviewer_session_id:
        raise SessionError("Worker and reviewer session IDs must be distinct")


def assert_resume_session_matches(expected_session_id: str, observed_session_id: str | None) -> None:
    if observed_session_id is None:
        return
    if observed_session_id != expected_session_id:
        raise SessionError(
            f"Resumed session ID mismatch: expected {expected_session_id}, got {observed_session_id}"
        )


def parse_stream_line(line: str, *, line_number: int) -> dict[str, Any]:
    if len(line.encode("utf-8")) > MAX_STREAM_LINE_BYTES:
        raise StreamParseError(
            f"Stream line {line_number} exceeds maximum size ({MAX_STREAM_LINE_BYTES} bytes)"
        )
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise StreamParseError(f"Malformed JSON on stream line {line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise StreamParseError(f"Stream line {line_number} must be a JSON object")
    return value


def _extract_text_from_event(event: dict[str, Any]) -> str:
    if event.get("type") == "result":
        result = event.get("result")
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            text = result.get("text") or result.get("content")
            if isinstance(text, str):
                return text
    if event.get("type") == "assistant":
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                return "".join(parts)
    return ""


def parse_cursor_stream(
    lines: Iterable[str],
    *,
    expected_session_id: str | None = None,
    fail_on_malformed: bool = True,
) -> CursorStreamParseResult:
    """Parse NDJSON Cursor output into session metadata and terminal text."""
    result = CursorStreamParseResult()
    text_parts: list[str] = []

    for line_number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            event = parse_stream_line(stripped, line_number=line_number)
        except StreamParseError as exc:
            if fail_on_malformed:
                raise
            result.diagnostics.append(
                StreamDiagnostic(
                    code=StreamIssueCode.MALFORMED_LINE,
                    message=str(exc),
                    line_number=line_number,
                )
            )
            continue

        result.events.append(event)
        if isinstance(event.get("request_id"), str):
            result.request_id = event["request_id"]
        if isinstance(event.get("model"), str):
            result.model = event["model"]
        if isinstance(event.get("usage"), dict):
            result.usage = event["usage"]

        session_id = event.get("session_id") or event.get("sessionId")
        if isinstance(session_id, str):
            if expected_session_id:
                try:
                    assert_resume_session_matches(expected_session_id, session_id)
                except SessionError as exc:
                    if fail_on_malformed:
                        raise
                    result.diagnostics.append(
                        StreamDiagnostic(
                            code=StreamIssueCode.SESSION_MISMATCH,
                            message=str(exc),
                            line_number=line_number,
                        )
                    )
            if result.session_id is None:
                result.session_id = session_id

        chunk = _extract_text_from_event(event)
        if chunk:
            if event.get("type") == "result":
                result.final_text = chunk
            else:
                text_parts.append(chunk)

    if not result.final_text and text_parts:
        result.final_text = "".join(text_parts)

    if expected_session_id and result.session_id is None:
        result.diagnostics.append(
            StreamDiagnostic(
                code=StreamIssueCode.MISSING_SESSION,
                message="No session_id captured from stream",
            )
        )
    if not result.final_text:
        result.diagnostics.append(
            StreamDiagnostic(
                code=StreamIssueCode.MISSING_RESULT,
                message="No terminal assistant result captured from stream",
            )
        )

    return result
