"""Console verbosity, structure, and color behavior."""

from io import StringIO
from pathlib import Path

from auto_loop.console_output import RunConsole
from auto_loop.providers.cursor import TraceEvent, TraceEventKind


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


def _console(level: str = "normal", *, color: bool | None = False) -> tuple[RunConsole, StringIO]:
    stream = StringIO()
    return RunConsole(level, stream=stream, color=color), stream


def test_quiet_suppresses_normal_messages():
    console, stream = _console("quiet")
    console.lifecycle_started("lc-1", goal_summary="Build a kanban board")
    console.turn_started(1, "worker", model="auto")
    console.provider_trace(TraceEvent(TraceEventKind.THINKING, text="hidden"))
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_START, tool_name="read_file", status="running")
    )
    console.review_requested("plan", "plan")
    console.review_requested("batch", "W01-W02")
    console.review_result("pass", "plan")
    console.plan_ready(".ai/auto-loop/plan.md")
    console.lifecycle_completed()
    assert stream.getvalue() == ""


def test_verbose_includes_session_resume_and_lifecycle_id():
    console, stream = _console("verbose")
    console.lifecycle_started("lc-1", goal_summary="Build a kanban board")
    console.session_resumed("abcd-1234-efgh-5678")
    text = stream.getvalue()
    assert "lc-1" in text
    assert "resumed" in text
    assert "abcd-123" in text


def test_normal_omits_verbose_metadata():
    console, stream = _console("normal")
    console.lifecycle_started("lc-1", goal_summary="Build a kanban board")
    console.session_resumed("abcd-1234-efgh-5678")
    text = stream.getvalue()
    assert "lc-1" not in text
    assert "resumed" not in text


def test_start_banner_mentions_goal_and_tool_managed_state():
    console, stream = _console()
    console.lifecycle_started(
        "lc-1",
        goal_summary="Build a kanban board",
        user_config_rel=".ai/run.yaml",
        resuming=False,
    )
    text = stream.getvalue()
    assert "AUTO LOOP" in text
    assert "Goal: Build a kanban board" in text
    assert "Config: .ai/run.yaml" in text
    assert "State: .ai/auto-loop/" in text
    assert "Planner" in text
    assert "STARTING" in text
    assert "WAITING" in text
    assert "lc-1" not in text
    assert "━" in text


def test_resume_progress_prints_running_and_stays_quiet():
    console, stream = _console()
    console.lifecycle_resumed_progress(
        "\n".join(
            [
                "Lifecycle resumed",
                "Phase: final handoff",
                "Session: worker",
                "Turn: 356",
                "Status: RUNNING",
            ]
        )
    )
    text = stream.getvalue()
    assert "Lifecycle resumed" in text
    assert "Phase: final handoff" in text
    assert "Session: worker" in text
    assert "Turn: 356" in text
    assert "Status: RUNNING" in text
    quiet, quiet_stream = _console("quiet")
    quiet.lifecycle_resumed_progress("Lifecycle resumed\nStatus: RUNNING")
    assert quiet_stream.getvalue() == ""


def test_resume_header_skips_role_table():
    console, stream = _console()
    console.lifecycle_started("lc-9", goal_summary="Keep going", resuming=True)
    text = stream.getvalue()
    assert "AUTO LOOP · RESUME" in text
    assert "Goal: Keep going" in text
    assert "STARTING" not in text
    assert "WAITING" not in text
    assert "lc-9" not in text


def test_plan_ready_points_at_generated_plan():
    console, stream = _console()
    console.plan_ready(".ai/auto-loop/plan.md")
    text = stream.getvalue()
    assert "PLAN APPROVED" in text
    assert ".ai/auto-loop/plan.md" in text
    assert "worker" in text


def test_turn_rules_use_stable_role_labels():
    console, stream = _console()
    for actor, label in (
        ("planner", "PLANNER"),
        ("plan_reviewer", "PLAN REVIEWER"),
        ("worker", "WORKER"),
        ("reviewer", "REVIEWER"),
        ("future_role", "FUTURE ROLE"),
    ):
        console.turn_started(1, actor, model="test-model")
        assert label in stream.getvalue()


def test_review_lines_keep_scope_verdict_and_findings():
    console, stream = _console()
    console.review_requested("batch", "git:def5678")
    console.review_result("pass", "plan")
    console.review_result("revise", "batch", 2)
    console.review_result("complete", "final")
    console.review_result("hold", "batch", 1)
    text = stream.getvalue()
    assert "Batch review" in text
    assert "requested · git:def5678" in text
    assert "Plan review" in text
    assert "PASS" in text
    assert "REVISE" in text
    assert "2 findings" in text
    assert "Final review" in text
    assert "COMPLETE" in text
    assert "HOLD" in text
    assert "1 finding" in text
    assert "requested · batch ·" not in text


def _line_starting(text: str, label: str) -> str:
    for line in text.splitlines():
        if line.startswith(label):
            return line
    raise AssertionError(f"missing row {label!r} in {text!r}")


def test_review_request_rows_are_operator_facing():
    console, stream = _console()
    console.review_requested("plan", "plan")
    console.review_requested("plan", "architecture-v2")
    console.review_requested("batch", "W01-W02")
    console.review_requested("final", "whole-task")
    text = stream.getvalue()
    assert _line_starting(text, "Plan review") == "Plan review   requested"
    assert "requested · plan · plan" not in text
    assert "requested · plan" not in text
    assert _line_starting(text, "Plan review   requested ·") == (
        "Plan review   requested · architecture-v2"
    )
    assert _line_starting(text, "Batch review") == "Batch review  requested · W01-W02"
    assert _line_starting(text, "Final review") == "Final review  requested · whole-task"


def test_review_result_rows_use_the_same_scope_labels():
    console, stream = _console()
    console.review_result("pass", "plan", 0)
    console.review_result("revise", "batch", 2)
    console.review_result("complete", "final", 0)
    console.review_result("blocked", "final", 0)
    text = stream.getvalue()
    assert _line_starting(text, "Plan review") == "Plan review   PASS"
    assert _line_starting(text, "Batch review") == "Batch review  REVISE · 2 findings"
    assert "Final review  COMPLETE" in text
    assert "Final review  BLOCKED" in text


def test_unknown_review_scope_falls_back_without_crashing():
    from auto_loop.console_output import review_scope_label

    assert review_scope_label("future_scope") == "Future scope review"
    assert review_scope_label("future-scope") == "Future scope review"
    assert review_scope_label("") == "Review"
    console, stream = _console()
    console.review_requested("future_scope", "alpha")
    console.review_result("pass", "future-scope")
    text = stream.getvalue()
    rows = [line for line in text.splitlines() if line.startswith("Future scope review")]
    assert rows == [
        "Future scope review requested · alpha",
        "Future scope review PASS",
    ]


def test_verbose_review_rows_stay_concise():
    console, stream = _console("verbose")
    console.review_requested("plan", "plan")
    console.review_requested("batch", "W01")
    console.review_result("pass", "plan")
    text = stream.getvalue()
    assert "Plan review   requested" in text
    assert "Batch review  requested · W01" in text
    assert "Plan review   PASS" in text
    assert "requested · plan · plan" not in text


def test_review_transition_sequence_stays_readable():
    console, stream = _console()
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="Planning complete."))
    console.finish_provider_trace()
    console.review_requested("plan", "plan")
    console.turn_started(2, "plan_reviewer", model="gpt-5.6")
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="Implementation batch ready."))
    console.finish_provider_trace()
    console.review_requested("batch", "W01-W02")
    console.turn_started(5, "reviewer", model="gpt-5.6")
    console.review_requested("final", "whole-task")
    text = stream.getvalue()
    plan_at = text.index("Planning complete.")
    plan_review_at = text.index("Plan review   requested")
    turn_at = text.index("Turn 2 · PLAN REVIEWER · gpt-5.6")
    batch_at = text.index("Batch review  requested · W01-W02")
    final_at = text.index("Final review  requested · whole-task")
    assert plan_at < plan_review_at < turn_at < batch_at
    assert "Turn 5 · REVIEWER · gpt-5.6" in text
    assert text.index("Turn 5 · REVIEWER · gpt-5.6") < final_at
    assert "requested · plan · plan" not in text


def test_review_rows_stay_plain_without_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _TTY()
    console = RunConsole("normal", stream=stream)
    console.review_requested("batch", "W01-W02")
    console.review_result("revise", "batch", 2)
    text = stream.getvalue()
    assert "Batch review  requested · W01-W02" in text
    assert "Batch review  REVISE · 2 findings" in text
    assert "\x1b[" not in text


def test_baseline_and_terminal_states_are_labeled():
    console, stream = _console()
    console.baseline_advanced("def5678abcdef")
    console.lifecycle_completed()
    console.lifecycle_blocked()
    text = stream.getvalue()
    assert "advanced" in text
    assert "def5678" in text
    assert "COMPLETE" in text
    assert "Lifecycle completed successfully" in text
    assert "BLOCKED" in text
    assert "Lifecycle blocked by reviewer" in text


def test_stopped_and_limit_terminal_separators():
    console, stream = _console()
    console.lifecycle_stopped("Run interrupted.\nResume with:\n  auto-loop resume RUN_CONFIG")
    console.lifecycle_limit_reached("max_turns")
    text = stream.getvalue()
    assert "STOPPED" in text
    assert "Run interrupted." in text
    assert "auto-loop resume" in text
    assert "LIMIT REACHED" in text
    assert "max_turns" in text


def test_external_text_is_literal_when_color_is_disabled():
    console, stream = _console(color=False)
    markup = "[bold red]not markup[/bold red]"
    console.lifecycle_started("lc-1", goal_summary=markup)
    console.review_requested("batch", markup)
    text = stream.getvalue()
    assert markup in text
    assert "\x1b[" not in text


def test_redirected_output_has_no_ansi():
    stream = StringIO()
    console = RunConsole("normal", stream=stream)
    console.lifecycle_started("lc-1", goal_summary="Plain")
    console.review_result("pass", "plan")
    text = stream.getvalue()
    assert "AUTO LOOP" in text
    assert "PASS" in text
    assert "\x1b[" not in text


def test_forced_color_styles_known_verdicts():
    stream = StringIO()
    console = RunConsole("normal", stream=stream, color=True)
    console.lifecycle_started("lc-1", goal_summary="Color")
    console.turn_started(1, "planner", model="claude-4.5-sonnet")
    console.turn_started(2, "plan_reviewer", model="gpt-5.6")
    console.turn_started(3, "worker", model="claude-4.5-sonnet")
    console.turn_started(4, "reviewer", model="gpt-5.6")
    console.review_result("pass", "plan")
    console.review_result("revise", "batch", 2)
    console.review_result("blocked", "batch")
    console.review_result("hold", "final")
    raw = stream.getvalue()
    assert "\x1b[" in raw
    assert "PASS" in raw
    assert "32" in raw
    assert "33" in raw
    assert "31" in raw
    assert "34" in raw
    assert "35" in raw
    assert "36" in raw
    hold_at = raw.rfind("HOLD")
    assert hold_at > 0
    assert "32" not in raw[hold_at - 8 : hold_at]


def test_no_color_disables_tty_styling(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _TTY()
    console = RunConsole("normal", stream=stream)
    console.review_result("pass", "plan")
    text = stream.getvalue()
    assert "PASS" in text
    assert "\x1b[" not in text


def test_tty_auto_color_emits_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _TTY()
    console = RunConsole("normal", stream=stream)
    console.review_result("pass", "plan")
    text = stream.getvalue()
    assert "PASS" in text
    assert "\x1b[" in text


def test_thinking_and_message_deltas_share_one_prefix():
    console, stream = _console()
    for chunk in ("I need ", "to inspect ", "the repository."):
        console.provider_trace(TraceEvent(TraceEventKind.THINKING, text=chunk))
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="hello "))
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="world"))
    console.finish_provider_trace()
    assert stream.getvalue() == (
        "[thinking] I need to inspect the repository.\n[message] hello world\n"
    )


def test_tool_events_close_an_open_text_line():
    console, stream = _console()
    console.provider_trace(TraceEvent(TraceEventKind.THINKING, text="wait"))
    console.provider_trace(
        TraceEvent(
            TraceEventKind.TOOL_START,
            tool_name="read_file",
            status="running",
            args={"path": "src/a.py"},
        )
    )
    console.provider_trace(
        TraceEvent(
            TraceEventKind.TOOL_END,
            tool_name="read_file",
            status="completed",
            result="x" * 12,
        )
    )
    console.provider_trace(TraceEvent(TraceEventKind.THINKING, text="now"))
    console.finish_provider_trace()
    assert stream.getvalue() == (
        "[thinking] wait\n"
        '[tool:start] read_file  src/a.py\n'
        "[tool:end]   read_file  completed · 12 chars\n"
        "[thinking] now\n"
    )


def test_provider_trace_colors_match_event_kind():
    stream = StringIO()
    console = RunConsole("normal", stream=stream, color=True)
    console.provider_trace(TraceEvent(TraceEventKind.THINKING, text="think"))
    console.finish_provider_trace()
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="say"))
    console.finish_provider_trace()
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_START, tool_name="read_file", status="running")
    )
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_END, tool_name="read_file", status="completed")
    )
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_END, tool_name="read_file", status="error")
    )
    raw = stream.getvalue()
    assert "[thinking]" in raw
    assert "[message]" in raw
    assert "[tool:start]" in raw
    assert raw.count("[tool:end]") == 2
    assert "\x1b[2m" in raw
    assert "97" in raw
    assert "33" in raw
    assert "32" in raw
    assert "31" in raw


def test_verbose_trace_includes_longer_tool_args():
    command = "p" * 500
    event = TraceEvent(
        TraceEventKind.TOOL_START,
        tool_name="run_terminal_cmd",
        status="running",
        args={"command": command},
    )
    normal, normal_stream = _console("normal")
    verbose, verbose_stream = _console("verbose")
    normal.provider_trace(event)
    verbose.provider_trace(event)
    normal_text = normal_stream.getvalue()
    verbose_text = verbose_stream.getvalue()
    assert "..." in normal_text
    assert command not in normal_text
    assert command in verbose_text


def test_no_color_keeps_trace_labels_without_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = _TTY()
    console = RunConsole("normal", stream=stream)
    console.provider_trace(TraceEvent(TraceEventKind.THINKING, text="secret"))
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_END, tool_name="read_file", status="error", result="boom")
    )
    console.finish_provider_trace()
    text = stream.getvalue()
    assert "[thinking] secret" in text
    assert "[tool:end]   read_file  failed · boom" in text
    assert "\x1b[" not in text


def test_turn_header_includes_model_for_all_slots_in_normal_and_verbose():
    for level in ("normal", "verbose"):
        console, stream = _console(level)
        console.turn_started(1, "planner", model="planner-model")
        console.turn_started(2, "plan_reviewer", model="reviewer-model")
        console.turn_started(3, "worker", model="worker-model")
        console.turn_started(4, "reviewer", model="reviewer-model")
        text = stream.getvalue()
        assert "Turn 1 · PLANNER · planner-model" in text
        assert "Turn 2 · PLAN REVIEWER · reviewer-model" in text
        assert "Turn 3 · WORKER · worker-model" in text
        assert "Turn 4 · REVIEWER · reviewer-model" in text


def test_turn_header_model_suppressed_in_quiet_mode():
    console, stream = _console("quiet")
    console.turn_started(3, "worker", model="auto")
    assert stream.getvalue() == ""


def test_turn_header_shows_auto_literal():
    console, stream = _console()
    console.turn_started(3, "worker", model="auto")
    assert "Turn 3 · WORKER · auto" in stream.getvalue()


def test_resolved_model_uses_session_model_for_resumed_slot(tmp_path: Path):
    from auto_loop.init_cmd import bootstrap_workspace
    from auto_loop.lifecycle import create_lifecycle
    from auto_loop.loop import LifecycleRunner
    from auto_loop.providers.scripted import ScriptedProvider
    from auto_loop.run_options import RunOptions
    from auto_loop.config import default_config

    repo = tmp_path / "repo"
    repo.mkdir()
    bootstrap_workspace(repo)
    options = RunOptions(
        "auto",
        "gpt-5.6",
        max_turns=1,
        max_runtime_minutes=60,
        verbose=False,
        quiet=True,
        planner_model="claude-4.5-sonnet",
    )
    runner = LifecycleRunner(repo, default_config(), options, ScriptedProvider())
    state = create_lifecycle("abc123")
    state.sessions["worker"].session_id = "worker-session"
    state.sessions["worker"].model = "claude-4.5-sonnet"
    assert runner._resolved_model("worker", state) == "claude-4.5-sonnet"
    assert runner._resolved_model("planner", state) == "claude-4.5-sonnet"
    assert runner._resolved_model("plan_reviewer", state) == "gpt-5.6"
    assert runner._resolved_model("reviewer", state) == "gpt-5.6"


def test_render_log_header_includes_optional_model():
    from auto_loop.console_output import render_log_header

    with_model = render_log_header(3, "worker", model="auto", color=False)
    assert "WORKER · auto" in with_model
    assert with_model.count(" · ") >= 2
    without_model = render_log_header(1, "planner", color=False)
    assert "PLANNER" in without_model
    assert without_model.count(" · ") == 1


def test_redirected_provider_trace_has_no_ansi():
    console, stream = _console()
    console.provider_trace(TraceEvent(TraceEventKind.MESSAGE, text="plain"))
    console.provider_trace(
        TraceEvent(TraceEventKind.TOOL_START, tool_name="read_file", status="running")
    )
    console.finish_provider_trace()
    text = stream.getvalue()
    assert "[message] plain" in text
    assert "[tool:start] read_file" in text
    assert "\x1b[" not in text
