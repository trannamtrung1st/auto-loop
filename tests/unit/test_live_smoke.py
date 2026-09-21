"""Live smoke gate and evidence helpers (no real Cursor)."""

from auto_loop.exits import ExitCode
from auto_loop.live_smoke import assert_smoke_success, live_smoke_gate
from auto_loop.live_smoke import SmokeEvidence


def test_live_smoke_skips_without_opt_in(monkeypatch):
    monkeypatch.delenv("AUTO_LOOP_LIVE_CURSOR", raising=False)
    gate = live_smoke_gate()
    assert not gate.enabled
    assert "AUTO_LOOP_LIVE_CURSOR" in gate.skip_reason


def test_assert_smoke_success_requires_complete():
    evidence = SmokeEvidence(
        worker_session_id="w",
        reviewer_session_id="r",
        plan_review_before_product_commits=True,
        completion_matches_head=True,
        review_count=3,
        exit_code=int(ExitCode.LIMIT_REACHED),
        doctor_ok=True,
        notes=[],
    )
    try:
        assert_smoke_success(evidence)
    except AssertionError as exc:
        assert "COMPLETE" in str(exc)
    else:
        raise AssertionError("expected failure")
