"""Protocol result model validation tests."""

import pytest
from pydantic import ValidationError

from auto_loop.models import Finding, ReviewerResult, WorkerResult


def test_worker_result_minimal():
    result = WorkerResult(
        schema_version=1,
        actor="worker",
        status="review_requested",
        work_summary="done",
    )
    assert result.actor == "worker"


def test_reviewer_pass_rejects_findings():
    with pytest.raises(ValidationError, match="findings"):
        ReviewerResult(
            schema_version=1,
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
            schema_version=1,
            actor="reviewer",
            verdict="revise",
            scope="plan",
            target="plan",
            summary="needs work",
        )


def test_complete_requires_final_scope_and_whole_task():
    with pytest.raises(ValidationError):
        ReviewerResult(
            schema_version=1,
            actor="reviewer",
            verdict="complete",
            scope="batch",
            target="W01",
            summary="done",
            whole_task_reviewed=True,
        )
