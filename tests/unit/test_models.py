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
