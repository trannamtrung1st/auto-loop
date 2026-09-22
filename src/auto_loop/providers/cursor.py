"""Cursor CLI command construction and stream-json parsing."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
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

# Compact tool args/results on the console and in the readable turn log.
# Verbose runs may render a longer args/error excerpt; full payloads stay in JSONL.
TRACE_PAYLOAD_LIMIT = 400
TRACE_PAYLOAD_LIMIT_VERBOSE = 2000


class TraceEventKind(StrEnum):
    THINKING = "thinking"
    MESSAGE = "message"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"


@dataclass(frozen=True)
class TraceEvent:
    """One normalized provider-trace event. Tool payloads stay generic."""

    kind: TraceEventKind
    text: str = ""
    tool_name: str | None = None
    call_id: str | None = None
    status: str | None = None
    args: object | None = None
    result: object | None = None


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
            "--stream-partial-output",
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
                    # tool_use blocks are not terminal text; tool lifecycle is separate.
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
    """Parse NDJSON Cursor output into session metadata and terminal text.

    Only ``type: result`` supplies ``final_text``. Streaming assistant events are
    retained in ``events`` for diagnostics but never satisfy the terminal contract.
    """
    result = CursorStreamParseResult()

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

        if event.get("type") == "result":
            chunk = _extract_text_from_event(event)
            if chunk:
                result.final_text = chunk

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


_SKIPPED_ASSISTANT_BLOCKS = frozenset(
    {"tool_use", "tool_result", "tool_call", "input_json_delta"}
)
_THINKING_BLOCKS = frozenset({"thinking", "reasoning"})
_TOOL_START_STATUSES = frozenset({"running", "started", "in_progress"})
_TOOL_SUCCESS_STATUSES = frozenset({"completed", "complete", "success", "ok"})
_TOOL_ERROR_STATUSES = frozenset({"error", "failed", "failure", "cancelled", "canceled"})


def trace_text_prefix(kind: TraceEventKind) -> str:
    if kind is TraceEventKind.THINKING:
        return "[thinking] "
    if kind is TraceEventKind.MESSAGE:
        return "[message] "
    return ""


def trace_events_from_stream_line(
    line: str,
    *,
    legacy_assistant_trace: bool = False,
) -> list[TraceEvent]:
    """Map one Cursor NDJSON line to normalized trace events.

    Malformed lines yield an empty list. Tool rendering comes only from
    ``tool_call`` lifecycle events; assistant ``tool_use`` blocks are ignored
    so the same call is not shown twice.

    This uses a fresh assistant segment, so a buffered assistant copy is shown.
    Deduplicating that copy against earlier deltas requires one
    :class:`AssistantTraceNormalizer` for the whole stream.
    """
    return AssistantTraceNormalizer().events_from_line(
        line, legacy_assistant_trace=legacy_assistant_trace
    )


def format_tool_trace(event: TraceEvent, *, payload_limit: int = TRACE_PAYLOAD_LIMIT) -> str:
    """Single-line tool trace. Args are a short summary; results are not dumped."""
    name = event.tool_name or "tool"
    if event.kind is TraceEventKind.TOOL_START:
        rendered = _tool_arg_summary(event.args, payload_limit)
        if rendered:
            return f"[tool:start] {name}  {rendered}"
        return f"[tool:start] {name}"
    if event.status == "error":
        label = f"[tool:end]   {name}  failed"
        detail = _compact_payload(_error_text(event.result), payload_limit)
        if detail:
            return f"{label} · {detail}"
        return label
    label = f"[tool:end]   {name}  completed"
    count = _payload_chars(event.result)
    if count:
        return f"{label} · {count} chars"
    return label


def _parse_stream_object(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _trace_events_from_object(event: dict[str, Any]) -> list[TraceEvent]:
    event_type = event.get("type") or event.get("event")
    if not isinstance(event_type, str):
        return []
    if event_type == "thinking":
        text = _first_str(event, "text", "thinking", "delta", "content")
        if not text:
            return []
        return [TraceEvent(kind=TraceEventKind.THINKING, text=text)]
    if event_type == "tool_call":
        tool = _tool_trace_event(event)
        return [tool] if tool is not None else []
    return []


def _assistant_is_live_delta(event: dict[str, Any]) -> bool:
    """Incremental assistant chunk from ``--stream-partial-output``.

    Timestamped assistant events without ``model_call_id`` are live deltas.
    Events that carry ``model_call_id`` are buffered copies of text already
    produced for that model call.
    """
    if _model_call_id(event) is not None:
        return False
    return event.get("timestamp_ms") is not None


def _model_call_id(event: dict[str, Any]) -> str | None:
    value = event.get("model_call_id")
    if isinstance(value, str) and value:
        return value
    return None


def _unseen_text(already: str, incoming: str) -> str:
    """Return assistant snapshot text that has not already been shown."""
    if not incoming:
        return ""
    if not already:
        return incoming
    if incoming == already or already.startswith(incoming):
        return ""
    if incoming.startswith(already):
        return incoming[len(already) :]
    return incoming


@dataclass
class AssistantTraceNormalizer:
    """Show each assistant segment once across Cursor's stream shapes.

    Live deltas render immediately. A later buffered copy is skipped when it
    repeats that text. When a segment never produced deltas, the buffered copy
    or the final assistant event is rendered once so the console still shows
    ``[message]``. ``result`` events are not trace output; protocol parsing
    keeps using them.
    """

    saw_delta: bool = False
    emitted_message: str = ""
    emitted_thinking: str = ""

    def reset_segment(self) -> None:
        self.saw_delta = False
        self.emitted_message = ""
        self.emitted_thinking = ""

    def events_from_line(
        self,
        line: str,
        *,
        legacy_assistant_trace: bool = False,
    ) -> list[TraceEvent]:
        event = _parse_stream_object(line)
        if event is None:
            return []
        event_type = event.get("type") or event.get("event")
        if event_type == "system" and event.get("subtype") in {"init", "start"}:
            self.reset_segment()
        if event_type == "tool_call":
            self.reset_segment()
        if event_type == "assistant":
            return self._assistant_events(event, legacy_assistant_trace=legacy_assistant_trace)
        return _trace_events_from_object(event)

    def _assistant_events(
        self,
        event: dict[str, Any],
        *,
        legacy_assistant_trace: bool,
    ) -> list[TraceEvent]:
        parsed = _assistant_partial_text_events(event)
        if not parsed:
            return []
        if _assistant_is_live_delta(event):
            self.saw_delta = True
            self._remember(parsed)
            return parsed
        if _model_call_id(event) is not None and (legacy_assistant_trace or self.saw_delta):
            return []
        return self._fallback(parsed)

    def _remember(self, events: list[TraceEvent]) -> None:
        for event in events:
            if event.kind is TraceEventKind.THINKING:
                self.emitted_thinking += event.text
            elif event.kind is TraceEventKind.MESSAGE:
                self.emitted_message += event.text

    def _fallback(self, parsed: list[TraceEvent]) -> list[TraceEvent]:
        thinking = "".join(event.text for event in parsed if event.kind is TraceEventKind.THINKING)
        message = "".join(event.text for event in parsed if event.kind is TraceEventKind.MESSAGE)
        unseen_thinking = _unseen_text(self.emitted_thinking, thinking)
        unseen_message = _unseen_text(self.emitted_message, message)
        emitted: list[TraceEvent] = []
        if unseen_thinking:
            emitted.append(TraceEvent(kind=TraceEventKind.THINKING, text=unseen_thinking))
            self.emitted_thinking += unseen_thinking
        if unseen_message:
            emitted.append(TraceEvent(kind=TraceEventKind.MESSAGE, text=unseen_message))
            self.emitted_message += unseen_message
        return emitted


def _assistant_partial_text_events(event: dict[str, Any]) -> list[TraceEvent]:
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        from_content = _content_trace_events(content)
        if from_content:
            return from_content
    elif isinstance(message, str) and message:
        return [TraceEvent(kind=TraceEventKind.MESSAGE, text=message)]
    direct = event.get("text")
    if isinstance(direct, str) and direct:
        return [TraceEvent(kind=TraceEventKind.MESSAGE, text=direct)]
    if isinstance(direct, list):
        return _content_trace_events(direct)
    content = event.get("content")
    return _content_trace_events(content)


def _content_trace_events(content: object) -> list[TraceEvent]:
    if isinstance(content, str):
        if not content:
            return []
        return [TraceEvent(kind=TraceEventKind.MESSAGE, text=content)]
    if not isinstance(content, list):
        return []
    events: list[TraceEvent] = []
    for item in content:
        if isinstance(item, str):
            if item:
                events.append(TraceEvent(kind=TraceEventKind.MESSAGE, text=item))
            continue
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type in _SKIPPED_ASSISTANT_BLOCKS:
            continue
        if block_type in _THINKING_BLOCKS:
            text = _first_str(item, "thinking", "text", "content")
            if text:
                events.append(TraceEvent(kind=TraceEventKind.THINKING, text=text))
            continue
        if block_type in ("text", None):
            text = item.get("text")
            if isinstance(text, str) and text:
                events.append(TraceEvent(kind=TraceEventKind.MESSAGE, text=text))
    return events


def _tool_trace_event(event: dict[str, Any]) -> TraceEvent | None:
    name, call_id, args, result = _tool_fields(event)
    status = _tool_status(event)
    if status in _TOOL_START_STATUSES:
        return TraceEvent(
            kind=TraceEventKind.TOOL_START,
            tool_name=name,
            call_id=call_id,
            status="running",
            args=args,
            result=result,
        )
    if status in _TOOL_SUCCESS_STATUSES and _result_is_error(result):
        status = "error"
    if status in _TOOL_ERROR_STATUSES or status == "error":
        return TraceEvent(
            kind=TraceEventKind.TOOL_END,
            tool_name=name,
            call_id=call_id,
            status="error",
            args=args,
            result=result,
        )
    if status in _TOOL_SUCCESS_STATUSES:
        return TraceEvent(
            kind=TraceEventKind.TOOL_END,
            tool_name=name,
            call_id=call_id,
            status="completed",
            args=args,
            result=result,
        )
    return None


_TOOL_METADATA_KEYS = frozenset(
    {
        "hookAdditionalContexts",
        "toolCallId",
        "startedAtMs",
        "completedAtMs",
    }
)
_ARG_NOISE_KEYS = frozenset(
    {
        "toolCallId",
        "simpleCommands",
        "timeout",
        "workingDirectory",
        "caseInsensitive",
        "multiline",
        "headLimit",
        "offset",
    }
)
_CURSOR_TOOL_NAMES = {
    "readToolCall": "read_file",
    "grepToolCall": "grep",
    "shellToolCall": "run_terminal_cmd",
    "globToolCall": "glob",
    "writeToolCall": "write_file",
    "editToolCall": "edit_file",
    "strReplaceToolCall": "edit_file",
    "deleteToolCall": "delete_file",
    "updateTodosToolCall": "update_todos",
    "getMcpToolsToolCall": "get_mcp_tools",
}
_SUMMARY_KEYS = ("path", "file_path", "target_file", "command", "pattern", "globPattern", "glob_pattern", "query")


def _display_tool_name(raw: str) -> str:
    mapped = _CURSOR_TOOL_NAMES.get(raw)
    if mapped:
        return mapped
    if raw.endswith("ToolCall") and len(raw) > len("ToolCall"):
        stem = raw[: -len("ToolCall")]
        parts: list[str] = []
        chunk = ""
        for char in stem:
            if char.isupper() and chunk:
                parts.append(chunk)
                chunk = char.lower()
            else:
                chunk += char.lower()
        if chunk:
            parts.append(chunk)
        if parts:
            return "_".join(parts)
    return raw


def _tool_wrapper(nested: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    wrappers: list[tuple[str, dict[str, Any]]] = []
    for key, value in nested.items():
        if key in _TOOL_METADATA_KEYS or not isinstance(value, dict):
            continue
        if key.endswith("ToolCall") or "args" in value or "result" in value:
            wrappers.append((key, value))
    if not wrappers:
        return None
    for key, value in wrappers:
        if key.endswith("ToolCall"):
            return key, value
    return wrappers[0]


def _tool_arg_summary(args: object, limit: int) -> str:
    if not isinstance(args, dict):
        return _compact_payload(args, limit)
    useful = {
        key: value
        for key, value in args.items()
        if key not in _ARG_NOISE_KEYS and value not in (None, "", [], {})
    }
    highlights = [(key, useful[key]) for key in _SUMMARY_KEYS if key in useful]
    if len(highlights) == 1:
        return _compact_payload(highlights[0][1], limit)
    if highlights:
        rendered = " ".join(
            f"{key}={_compact_payload(value, max(limit // len(highlights), 16))}"
            for key, value in highlights
        )
        return _compact_payload(rendered, limit)
    if not useful:
        return ""
    return _compact_payload(useful, limit)


def _tool_fields(
    event: dict[str, Any],
) -> tuple[str, str | None, object | None, object | None]:
    call_id = event.get("call_id") or event.get("tool_call_id") or event.get("id")
    if not isinstance(call_id, str):
        call_id = None
    name = event.get("name") or event.get("tool_name") or event.get("tool")
    args = event.get("args")
    if args is None:
        args = event.get("arguments")
    result = event.get("result")
    nested = event.get("tool_call")
    if isinstance(nested, dict) and nested:
        if not isinstance(name, str):
            inner_name = nested.get("name") or nested.get("tool_name")
            if isinstance(inner_name, str):
                name = inner_name
        wrapped = _tool_wrapper(nested)
        if wrapped is not None:
            wrapper_name, body = wrapped
            if not isinstance(name, str):
                name = wrapper_name
            if args is None and isinstance(body.get("args"), (dict, str, list)):
                args = body.get("args")
            if result is None and "result" in body:
                result = body.get("result")
    if not isinstance(name, str) or not name:
        name = "tool"
    else:
        name = _display_tool_name(name)
    return name, call_id, args, result


def _tool_status(event: dict[str, Any]) -> str | None:
    status = event.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    subtype = event.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        raw = subtype.strip().lower()
        if raw in {"started", "start"}:
            return "running"
        return raw
    return None


def _result_is_error(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    error = result.get("error")
    return error not in (None, False, "")


def _first_str(event: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _error_text(result: object) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    error = result.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("message", "errorMessage", "error"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("message", "errorMessage"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _compact_payload(value: object, limit: int) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(value)
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _payload_chars(value: object) -> int:
    if value is None or value == "" or value == {} or value == []:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))
