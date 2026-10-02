"""Protocol result model validation tests."""

import pytest
from pydantic import ValidationError

from auto_loop.models import Finding, PlannerResult, ROLE_FOR_SLOT, ReviewerResult, WorkerResult


def test_planner_result_minimal():
    result = PlannerResult(
        schema_version=2,
        actor="planner",
        status="review_requested",
        review={"scope": "plan", "target": "plan", "summary": "ready"},
        plan_summary="planned",
    )
    assert result.actor == "planner"


def test_planner_rejects_non_plan_scope():
    with pytest.raises(ValidationError, match="scope=plan"):
        PlannerResult(
            schema_version=2,
            actor="planner",
            status="review_requested",
            review={"scope": "batch", "target": "W01", "summary": "nope"},
            plan_summary="planned",
        )


def test_role_for_slot_mapping():
    assert ROLE_FOR_SLOT["planner"] == "planner"
    assert ROLE_FOR_SLOT["plan_reviewer"] == "reviewer"
    assert ROLE_FOR_SLOT["worker"] == "worker"
    assert ROLE_FOR_SLOT["reviewer"] == "reviewer"


def test_worker_result_minimal():
    result = WorkerResult(
        schema_version=2,
        actor="worker",
        status="review_requested",
        work_summary="done",
    )
    assert result.actor == "worker"


def test_worker_replan_requested_requires_reason_and_rejects_review():
    result = WorkerResult(
        schema_version=2,
        actor="worker",
        status="replan_requested",
        replan={"reason": "The persistence assumption is false."},
        work_summary="Need a new strategy.",
    )
    assert result.replan is not None
    assert result.review is None
    with pytest.raises(ValidationError, match="replan.reason"):
        WorkerResult(
            schema_version=2,
            actor="worker",
            status="replan_requested",
            replan={"reason": "  "},
            work_summary="empty",
        )
    with pytest.raises(ValidationError, match="must not include a review"):
        WorkerResult(
            schema_version=2,
            actor="worker",
            status="replan_requested",
            review={"scope": "plan", "target": "plan", "summary": "nope"},
            replan={"reason": "strategy failed"},
            work_summary="mixed",
        )
    with pytest.raises(ValidationError, match="only valid when status is replan_requested"):
        WorkerResult(
            schema_version=2,
            actor="worker",
            status="blocked",
            replan={"reason": "not a replan"},
            work_summary="blocked",
        )


def test_reviewer_pass_rejects_findings():
    with pytest.raises(ValidationError, match="findings"):
        ReviewerResult(
            schema_version=2,
            actor="reviewer",
            verdict="pass",
            scope="batch",
            target="W01",
            summary="ok",
            findings=[
                Finding(
                    id="F1",
                    title="t",
                    detail="d",
                    evidence="e",
                    required_change="c",
                )
            ],
        )


def test_reviewer_revise_requires_findings():
    with pytest.raises(ValidationError, match="finding"):
        ReviewerResult(
            schema_version=2,
            actor="reviewer",
            verdict="revise",
            scope="plan",
            target="plan",
            summary="needs work",
        )


def test_false_blocker_endgame_matches_contracted_finding_id_only():
    from auto_loop.models import FALSE_BLOCKER_ENDGAME_FINDING_ID, is_false_blocker_endgame_revise

    finding = {
        "id": FALSE_BLOCKER_ENDGAME_FINDING_ID,
        "title": "Request final review",
        "detail": "No in-scope work remains.",
        "evidence": "task.md",
        "required_change": "Request scope=final.",
    }
    revise = ReviewerResult(
        schema_version=2,
        actor="reviewer",
        verdict="revise",
        scope="batch",
        target="blocked",
        summary="not an external blocker",
        findings=[finding],
    )
    assert is_false_blocker_endgame_revise(
        revise, target="blocked", session_purpose="reviewer"
    )
    prose_only = ReviewerResult(
        schema_version=2,
        actor="reviewer",
        verdict="revise",
        scope="batch",
        target="blocked",
        summary="not an external blocker",
        findings=[{**finding, "id": "f-1", "title": FALSE_BLOCKER_ENDGAME_FINDING_ID}],
    )
    assert not is_false_blocker_endgame_revise(
        prose_only, target="blocked", session_purpose="reviewer"
    )
    assert not is_false_blocker_endgame_revise(
        revise, target="blocked", session_purpose="plan_reviewer"
    )
    assert not is_false_blocker_endgame_revise(
        revise, target="W01", session_purpose="reviewer"
    )


def test_complete_requires_final_scope_and_whole_task():
    with pytest.raises(ValidationError):
        ReviewerResult(
            schema_version=2,
            actor="reviewer",
            verdict="complete",
            scope="batch",
            target="W01",
            summary="done",
            whole_task_reviewed=True,
        )
