"""Console verbosity, structure, and color behavior."""

from io import StringIO

from auto_loop.console_output import RunConsole


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


def _console(level: str = "normal", *, color: bool | None = False) -> tuple[RunConsole, StringIO]:
    stream = StringIO()
    return RunConsole(level, stream=stream, color=color), stream


def test_quiet_suppresses_normal_messages():
    console, stream = _console("quiet")
    console.lifecycle_started("lc-1", goal_summary="Build a kanban board")
    console.turn_started(1, "worker")
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
        console.turn_started(1, actor)
        assert label in stream.getvalue()


def test_review_lines_keep_scope_verdict_and_findings():
    console, stream = _console()
    console.review_requested("batch", "git:def5678")
    console.review_result("pass", "plan")
    console.review_result("revise", "batch", 2)
    console.review_result("complete", "final")
    console.review_result("hold", "batch", 1)
    text = stream.getvalue()
    assert "requested" in text
    assert "batch" in text
    assert "git:def5678" in text
    assert "PASS" in text
    assert "REVISE" in text
    assert "2 findings" in text
    assert "COMPLETE" in text
    assert "HOLD" in text
    assert "1 finding" in text


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
    console.turn_started(1, "planner")
    console.turn_started(2, "plan_reviewer")
    console.turn_started(3, "worker")
    console.turn_started(4, "reviewer")
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
