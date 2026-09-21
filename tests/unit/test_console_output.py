"""Console verbosity behavior."""

from io import StringIO

from auto_loop.console_output import RunConsole


def test_quiet_suppresses_normal_messages():
    stream = StringIO()
    console = RunConsole("quiet", stream=stream)
    console.turn_started(1, "worker")
    console.review_result("pass", "plan")
    assert stream.getvalue() == ""


def test_verbose_includes_session_resume():
    stream = StringIO()
    console = RunConsole("verbose", stream=stream)
    console.session_resumed("worker", "abcd-1234-efgh-5678")
    assert "resuming session" in stream.getvalue()


def test_start_banner_mentions_goal_and_tool_managed_state():
    stream = StringIO()
    console = RunConsole("normal", stream=stream)
    console.lifecycle_started(
        "lc-1",
        goal_summary="Build a kanban board",
        user_config_rel=".ai/run.yaml",
        resuming=False,
    )
    text = stream.getvalue()
    assert "Starting Auto Loop" in text
    assert "Goal: Build a kanban board" in text
    assert "Config: .ai/run.yaml" in text
    assert ".ai/auto-loop/" in text
    assert "lc-1" not in text


def test_plan_ready_points_at_generated_plan():
    stream = StringIO()
    console = RunConsole("normal", stream=stream)
    console.plan_ready(".ai/auto-loop/plan.md")
    assert "Plan ready: .ai/auto-loop/plan.md" in stream.getvalue()
