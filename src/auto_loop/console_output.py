"""Run console rendering for quiet/normal/verbose modes.

Presentation only: lifecycle code calls semantic methods, and this module owns
Rich styles, rules, and literal handling of external strings.
"""

from __future__ import annotations

import os
import sys
from io import StringIO
from typing import TextIO

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from auto_loop.config import ConsoleLevel
from auto_loop.result_trace_filter import ResultTraceFilter
from auto_loop.providers.cursor import (
    TRACE_PAYLOAD_LIMIT,
    TRACE_PAYLOAD_LIMIT_VERBOSE,
    TraceEvent,
    TraceEventKind,
    format_tool_trace,
)
from auto_loop.trace_text_block import TraceTextBlock

_LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}
_LABEL_WIDTH = 14
_RULE_CHAR = "━"
_HEADING_STYLE = "bold bright_cyan"
_META_STYLE = "dim"
_NEUTRAL_BOLD = "bold"

ROLE_STYLES = {
    "planner": "bold blue",
    "plan_reviewer": "bold magenta",
    "worker": "bold cyan",
    "reviewer": "bold yellow",
}

ROLE_LABELS = {
    "planner": "PLANNER",
    "plan_reviewer": "PLAN REVIEWER",
    "worker": "WORKER",
    "reviewer": "REVIEWER",
}

_REVIEW_SCOPE_LABELS = {
    "plan": "Plan review",
    "batch": "Batch review",
    "final": "Final review",
}

STATUS_STYLES = {
    "pass": "bold green",
    "complete": "bold green",
    "revise": "bold yellow",
    "blocked": "bold red",
    "error": "bold red",
    "failure": "bold red",
}

_TRACE_TEXT_STYLES = {
    TraceEventKind.THINKING: "dim",
    TraceEventKind.MESSAGE: "bright_white",
}


def role_label(actor: str) -> str:
    known = ROLE_LABELS.get(actor)
    if known is not None:
        return known
    cleaned = actor.replace("_", " ").replace("-", " ").strip()
    return cleaned.upper() if cleaned else "UNKNOWN"


def role_style(actor: str) -> str:
    return ROLE_STYLES.get(actor, _NEUTRAL_BOLD)


def review_scope_label(scope: str) -> str:
    """Operator label for a review scope. Unknown scopes stay readable."""
    known = _REVIEW_SCOPE_LABELS.get(scope)
    if known is not None:
        return known
    cleaned = scope.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return "Review"
    titled = cleaned[0].upper() + cleaned[1:]
    return f"{titled} review"


def _review_row_label(scope: str) -> str:
    label = review_scope_label(scope)
    if len(label) >= _LABEL_WIDTH:
        return f"{label} "
    return label


def _review_request_target(scope: str, target: str) -> str:
    """Target suffix for a review request. Omit redundant plan/plan."""
    cleaned = _single_line(target).strip()
    if not cleaned:
        return ""
    if scope == "plan" and cleaned == "plan":
        return ""
    return cleaned


def status_style(verdict: str) -> str:
    return STATUS_STYLES.get(verdict.lower(), _NEUTRAL_BOLD)


def _single_line(value: str) -> str:
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _color_disabled_by_env() -> bool:
    # Any NO_COLOR value, including empty, requests plain text.
    return "NO_COLOR" in os.environ


def make_rich_console(stream: TextIO, *, color: bool | None) -> Console:
    """Build a console that styles literal ``Text`` and never parses markup."""
    if color is None and _color_disabled_by_env():
        color = False
    if color is True:
        return Console(
            file=stream,
            force_terminal=True,
            no_color=False,
            color_system="standard",
            highlight=False,
            markup=False,
            emoji=False,
            soft_wrap=True,
        )
    if color is False:
        return Console(
            file=stream,
            force_terminal=False,
            no_color=True,
            color_system=None,
            highlight=False,
            markup=False,
            emoji=False,
            soft_wrap=True,
        )
    return Console(
        file=stream,
        highlight=False,
        markup=False,
        emoji=False,
        soft_wrap=True,
    )


def console_color_enabled(stream: TextIO | None = None) -> bool:
    """True when ``stream`` (stdout by default) should receive ANSI styles."""
    if _color_disabled_by_env():
        return False
    target = sys.stdout if stream is None else stream
    probe = Console(file=target, highlight=False, markup=False, emoji=False)
    return bool(probe.is_terminal and probe.color_system is not None)


def render_log_header(
    turn: int,
    role: str,
    *,
    model: str | None = None,
    color: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """Auto Loop framing for one provider log. The returned text excludes log bytes."""
    buffer = StringIO()
    if color is None:
        target = sys.stdout if stream is None else stream
        enabled = console_color_enabled(target)
    else:
        enabled = color
    console = make_rich_console(buffer, color=enabled)
    title = Text()
    title.append(f"turn {turn:04d} · ")
    title.append(role_label(role), style=role_style(role))
    if model:
        title.append(" · ")
        title.append(_single_line(model), style=_META_STYLE)
    console.print(Rule(title, style=role_style(role), characters=_RULE_CHAR))
    return buffer.getvalue()


class RunConsole:
    def __init__(
        self,
        level: ConsoleLevel,
        stream: TextIO | None = None,
        *,
        color: bool | None = None,
    ) -> None:
        self.level = level
        self.stream = stream or sys.stdout
        self._rich = make_rich_console(self.stream, color=color)
        self._text_block = TraceTextBlock()
        self._result_trace_filter = ResultTraceFilter()

    def _enabled(self, min_level: ConsoleLevel) -> bool:
        return _LEVELS[self.level] >= _LEVELS[min_level]

    def _blank(self, min_level: ConsoleLevel = "normal") -> None:
        if not self._enabled(min_level):
            return
        self._finish_stream_line()
        self._rich.print()

    def _rule(self, title: Text, *, style: str, min_level: ConsoleLevel = "normal") -> None:
        if not self._enabled(min_level):
            return
        self._finish_stream_line()
        self._rich.print(Rule(title, style=style, characters=_RULE_CHAR))

    def _row(self, label: str, value: Text, *, min_level: ConsoleLevel = "normal") -> None:
        if not self._enabled(min_level):
            return
        self._finish_stream_line()
        line = Text()
        line.append(label.ljust(_LABEL_WIDTH))
        line.append(value)
        self._rich.print(line)

    def _meta(
        self,
        label: str,
        value: str,
        *,
        value_style: str = "",
        min_level: ConsoleLevel = "normal",
    ) -> None:
        if not self._enabled(min_level):
            return
        self._finish_stream_line()
        line = Text()
        line.append(f"{label}: ", style="bold")
        line.append(_single_line(value), style=value_style)
        self._rich.print(line)

    def _status_row(self, label: str, status: str, *, style: str) -> None:
        if not self._enabled("normal"):
            return
        self._finish_stream_line()
        line = Text()
        line.append(label.ljust(_LABEL_WIDTH), style=style)
        line.append(status, style=style)
        self._rich.print(line)

    def _lifecycle_id(self, lifecycle_id: str, *, action: str) -> None:
        value = Text()
        value.append(_single_line(lifecycle_id), style=_META_STYLE)
        value.append(f" · {action}", style=_META_STYLE)
        self._row("Lifecycle", value, min_level="verbose")

    def lifecycle_started(
        self,
        lifecycle_id: str,
        *,
        goal_summary: str | None = None,
        user_config_rel: str = "run.yaml",
        resuming: bool = False,
        artifact_root_rel: str = ".ai/auto-loop",
    ) -> None:
        if resuming:
            title = Text("AUTO LOOP · RESUME", style=_HEADING_STYLE)
            self._rule(title, style=_HEADING_STYLE)
            if goal_summary:
                self._meta("Goal", goal_summary)
            self._lifecycle_id(lifecycle_id, action="resumed")
            return
        title = Text("AUTO LOOP", style=_HEADING_STYLE)
        self._rule(title, style=_HEADING_STYLE)
        if goal_summary:
            self._meta("Goal", goal_summary)
        self._meta("Config", user_config_rel, value_style=_META_STYLE)
        state = artifact_root_rel if artifact_root_rel.endswith("/") else f"{artifact_root_rel}/"
        self._meta("State", state, value_style=_META_STYLE)
        self._blank()
        self._status_row("Planner", "STARTING", style=role_style("planner"))
        self._status_row("Worker", "WAITING", style=_META_STYLE)
        self._status_row("Reviewer", "WAITING", style=_META_STYLE)
        self._lifecycle_id(lifecycle_id, action="started")

    def plan_ready(self, plan_path: str) -> None:
        if not self._enabled("normal"):
            return
        self._blank()
        title = Text("PLAN APPROVED", style="bold green")
        self._rule(title, style="bold green")
        self._row("Plan", Text(_single_line(plan_path), style=_META_STYLE))
        self._row("Next", Text("worker"))

    def turn_started(self, turn: int, actor: str, *, model: str) -> None:
        if not self._enabled("normal"):
            return
        self._result_trace_filter.reset()
        self._blank()
        title = Text()
        title.append(f"Turn {turn} · ")
        title.append(role_label(actor), style=role_style(actor))
        title.append(" · ")
        title.append(_single_line(model), style=_META_STYLE)
        self._rule(title, style=role_style(actor))

    def session_created(self, session_id: str) -> None:
        self._session_line(session_id, action="created", min_level="normal")

    def session_resumed(self, session_id: str) -> None:
        self._session_line(session_id, action="resumed", min_level="verbose")

    def _session_line(self, session_id: str, *, action: str, min_level: ConsoleLevel) -> None:
        short = _single_line(session_id)[:8]
        self._row("Session", Text(f"{short} · {action}", style=_META_STYLE), min_level=min_level)

    def review_requested(self, scope: str, target: str) -> None:
        value = Text()
        value.append("requested")
        shown = _review_request_target(scope, target)
        if shown:
            value.append(f" · {shown}", style=_META_STYLE)
        self._row(_review_row_label(scope), value)

    def review_result(self, verdict: str, scope: str, finding_count: int = 0) -> None:
        value = Text()
        value.append(verdict.upper(), style=status_style(verdict))
        if finding_count:
            noun = "finding" if finding_count == 1 else "findings"
            value.append(f" · {finding_count} {noun}", style=_META_STYLE)
        self._row(_review_row_label(scope), value)

    def baseline_advanced(self, head: str) -> None:
        value = Text()
        value.append("advanced", style="bold green")
        value.append(f" · {_single_line(head)[:7]}", style=_META_STYLE)
        self._row("Baseline", value)

    def lifecycle_completed(self) -> None:
        self._outcome("COMPLETE", "Lifecycle completed successfully", style="bold green")

    def lifecycle_blocked(self) -> None:
        self._outcome("BLOCKED", "Lifecycle blocked by reviewer", style="bold red")

    def lifecycle_stopped(self, message: str) -> None:
        if not self._enabled("normal"):
            return
        self._finish_stream_line()
        self._blank()
        self._rule(Text("STOPPED", style="bold yellow"), style="bold yellow")
        self._rich.print(Text(message))

    def lifecycle_limit_reached(self, reason: str) -> None:
        self._outcome("LIMIT REACHED", reason, style="bold yellow")

    def _outcome(self, title: str, message: str, *, style: str) -> None:
        if not self._enabled("normal"):
            return
        self._finish_stream_line()
        self._blank()
        self._rule(Text(title, style=style), style=style)
        self._rich.print(Text(_single_line(message)))

    def verbose(self, message: str) -> None:
        if not self._enabled("verbose"):
            return
        self._finish_stream_line()
        self._rich.print(Text(_single_line(message), style=_META_STYLE))

    def provider_trace(self, event: TraceEvent) -> None:
        """Render one normalized provider event immediately and flush."""
        if not self._enabled("normal"):
            return
        if event.kind in (TraceEventKind.THINKING, TraceEventKind.MESSAGE):
            self._trace_text(event)
            return
        if event.kind in (TraceEventKind.TOOL_START, TraceEventKind.TOOL_END):
            self._finish_message_trace_segment()
            limit = (
                TRACE_PAYLOAD_LIMIT_VERBOSE
                if self.level == "verbose"
                else TRACE_PAYLOAD_LIMIT
            )
            style = "bold yellow"
            if event.kind is TraceEventKind.TOOL_END:
                style = "bold red" if event.status == "error" else "bold green"
            self._rich.print(
                Text(format_tool_trace(event, payload_limit=limit, verbose=self.level == "verbose"), style=style)
            )
            self._rich.file.flush()

    def finish_provider_trace(self) -> None:
        """End an open thinking or message block so the next console row starts clean."""
        trailing = self._result_trace_filter.flush()
        if trailing:
            self._emit_trace_multiline(TraceEventKind.MESSAGE, trailing)
        self._result_trace_filter.reset()
        self._finish_stream_line()

    def _finish_message_trace_segment(self) -> None:
        trailing = self._result_trace_filter.flush_pending_outside()
        if trailing:
            self._emit_trace_multiline(TraceEventKind.MESSAGE, trailing)
        self._finish_stream_line()

    def _trace_text(self, event: TraceEvent) -> None:
        if event.kind is TraceEventKind.MESSAGE:
            filtered = self._result_trace_filter.feed(event.text)
            self._emit_trace_multiline(event.kind, filtered)
            return
        self._emit_trace_multiline(event.kind, event.text)

    def _emit_trace_multiline(self, kind: TraceEventKind, text: str) -> None:
        rendered = self._text_block.feed(kind, text)
        if not rendered:
            return
        style = _TRACE_TEXT_STYLES[kind]
        lines = rendered.split("\n")
        for index, line in enumerate(lines):
            if index:
                self._rich.file.write("\n")
            if line:
                self._rich.print(Text(line, style=style), end="")
        self._rich.file.flush()

    def _finish_stream_line(self) -> None:
        closing = self._text_block.close()
        if not closing:
            return
        self._rich.file.write(closing)
        self._rich.file.flush()
