"""Pre-validation sanitization for worker review handoffs."""

from auto_loop.models import ReviewRequest, WorkerResult
from auto_loop.protocol import (
    sanitize_agent_review_request,
    sanitize_worker_result_payload,
)


def test_sanitize_agent_review_request_strips_legacy_git_fields():
    sanitized, stripped = sanitize_agent_review_request(
        {
            "scope": "batch",
            "target": "W06",
            "summary": "batch ready",
            "targets": [
                {
                    "kind": "git_range",
                    "id": "git",
                    "base_commit": "abc1234",
                    "head_commit": "def5678",
                }
            ],
            "base_commit": "abc1234",
            "head_commit": "def5678",
        }
    )
    assert stripped is True
    assert ReviewRequest.model_validate(sanitized).targets == []
    assert "base_commit" not in sanitized
    assert "head_commit" not in sanitized


def test_sanitize_worker_result_payload_before_validation():
    payload, stripped = sanitize_worker_result_payload(
        {
            "schema_version": 2,
            "actor": "worker",
            "status": "review_requested",
            "review": {
                "scope": "batch",
                "target": "W01",
                "summary": "batch",
                "head_commit": "deadbeef",
            },
            "work_summary": "done",
        }
    )
    assert stripped is True
    result = WorkerResult.model_validate(payload)
    assert result.review is not None
    assert result.review.targets == []
