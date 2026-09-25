"""Pre-validation sanitization for worker review handoffs."""

import json

from auto_loop.models import ReviewRequest, WorkerResult
from auto_loop.protocol import (
    RESULT_BLOCK_END,
    RESULT_BLOCK_START,
    parse_worker_result,
    sanitize_agent_review_request,
    sanitize_worker_result_payload,
)


def _worker_payload(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "batch",
            "target": "W01",
            "summary": "batch",
        },
        "work_summary": "wrote plan",
        "verification": [],
        "notes": [],
    }
    base.update(overrides)
    return base


def _wrap(payload: dict) -> str:
    return f"noise\n{RESULT_BLOCK_START}\n{json.dumps(payload)}\n{RESULT_BLOCK_END}\n"


def test_sanitize_agent_review_request_strips_legacy_git_fields():
    sanitized = sanitize_agent_review_request(
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
    assert ReviewRequest.model_validate(sanitized).targets == []
    assert "base_commit" not in sanitized
    assert "head_commit" not in sanitized
    assert not any(
        isinstance(item, dict) and item.get("kind") == "git_range"
        for item in sanitized.get("targets", [])
    )


def test_sanitize_worker_result_payload_before_validation():
    payload = sanitize_worker_result_payload(
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
    assert "head_commit" not in payload["review"]
    result = WorkerResult.model_validate(payload)
    assert result.review is not None
    assert result.review.targets == []


def test_parse_worker_result_accepts_legacy_git_fields_in_raw_json():
    payload = _worker_payload()
    payload["review"]["targets"] = [
        {
            "kind": "git_range",
            "id": "git",
            "base_commit": "abc1234",
            "head_commit": "def5678",
        }
    ]
    payload["review"]["base_commit"] = "abc1234"
    payload["review"]["head_commit"] = "def5678"
    result = parse_worker_result(_wrap(payload))
    assert result.review is not None
    assert result.review.targets == []
