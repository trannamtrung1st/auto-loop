"""AUTO_LOOP_RESULT contract generation tests."""

import json

import pytest

from auto_loop.models import PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import (
    RESULT_BLOCK_END,
    RESULT_BLOCK_START,
    extract_result_blocks,
    parse_planner_result,
    parse_reviewer_result,
    parse_worker_result,
)
from auto_loop.result_contract import (
    canonical_result_payload,
    format_output_repair_protocol_contract,
    format_result_example,
    format_result_json_schema,
    format_single_result_example,
)


@pytest.mark.parametrize("role", ("planner", "worker", "reviewer"))
def test_output_repair_contract_includes_rules_and_example(role: str):
    text = format_output_repair_protocol_contract(role)
    assert "Required envelope for this turn" in text
    assert text.count(RESULT_BLOCK_START) == 1
    assert text.count(RESULT_BLOCK_END) == 1
    assert f'"actor": "{role}"' in text


@pytest.mark.parametrize("role", ("planner", "worker", "reviewer"))
def test_single_result_example_is_one_block(role: str):
    block = format_single_result_example(role)
    assert block.count(RESULT_BLOCK_START) == 1
    assert block.count(RESULT_BLOCK_END) == 1


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
    required = schema.get("required", [])
    assert "schema_version" in props
    assert "actor" in props
    assert "schema_version" in required
    assert "actor" in required


def test_example_parses_for_each_role():
    for role, parser in (
        ("planner", parse_planner_result),
        ("worker", parse_worker_result),
    ):
        block = format_result_example(role)
        assert block.startswith(RESULT_BLOCK_START)
        assert block.endswith(RESULT_BLOCK_END)
        parser(block)

    reviewer_examples = format_result_example("reviewer")
    assert "Example (batch PASS):" in reviewer_examples
    assert "Example (final COMPLETE" in reviewer_examples
    assert '"whole_task_reviewed": true' in reviewer_examples
    blocks = extract_result_blocks(reviewer_examples)
    assert len(blocks) == 2
    for block in blocks:
        parse_reviewer_result(block)
