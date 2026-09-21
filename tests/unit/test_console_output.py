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
