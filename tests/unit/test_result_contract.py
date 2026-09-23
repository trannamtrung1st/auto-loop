"""AUTO_LOOP_RESULT contract generation tests."""

import json

import pytest

from auto_loop.models import PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import (
    RESULT_BLOCK_END,
    RESULT_BLOCK_START,
    parse_planner_result,
    parse_reviewer_result,
    parse_worker_result,
)
from auto_loop.result_contract import (
    canonical_result_payload,
    format_result_example,
    format_result_json_schema,
)


@pytest.mark.parametrize("role", ("planner", "worker", "reviewer"))
def test_canonical_payload_validates(role: str):
    payload = canonical_result_payload(role)
    if role == "planner":
        PlannerResult.model_validate(payload)
    elif role == "worker":
        WorkerResult.model_validate(payload)
    else:
        ReviewerResult.model_validate(payload)


@pytest.mark.parametrize("role", ("planner", "worker", "reviewer"))
def test_json_schema_matches_model(role: str):
    schema = json.loads(format_result_json_schema(role))
    assert schema.get("title") in ("PlannerResult", "WorkerResult", "ReviewerResult")
    props = schema.get("properties", {})
    assert "schema_version" in props
    assert "actor" in props


def test_example_parses_for_each_role():
    for role, parser in (
        ("planner", parse_planner_result),
        ("worker", parse_worker_result),
        ("reviewer", parse_reviewer_result),
    ):
        block = format_result_example(role)
        assert block.startswith(RESULT_BLOCK_START)
        assert block.endswith(RESULT_BLOCK_END)
        parser(block)
