"""AUTO_LOOP_RESULT JSON contract derived from protocol Pydantic models."""

from __future__ import annotations

import json
from typing import Any, Type

from pydantic import BaseModel

from auto_loop.models import PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import RESULT_BLOCK_END, RESULT_BLOCK_START

Role = str

_RESULT_MODEL_FOR_ROLE: dict[Role, Type[BaseModel]] = {
    "planner": PlannerResult,
    "worker": WorkerResult,
    "reviewer": ReviewerResult,
}


def result_model_for_role(role: Role) -> Type[BaseModel]:
    try:
        return _RESULT_MODEL_FOR_ROLE[role]
    except KeyError:
        raise ValueError(f"Unknown instruction role: {role}") from None


def canonical_result_payload(role: Role) -> dict[str, Any]:
    """Minimal valid example object for the role; validated against the Pydantic model."""
    if role == "planner":
        payload = PlannerResult(
            status="review_requested",
            review={
                "scope": "plan",
                "target": "plan",
                "summary": "Plan ready for independent review.",
            },
            plan_summary="Outlined approach, scope, and verification strategy.",
        )
    elif role == "worker":
        payload = WorkerResult(
            status="review_requested",
            review={
                "scope": "batch",
                "target": "W01",
                "summary": "First implementation batch ready for review.",
                "targets": [
                    {
                        "kind": "path",
                        "id": "src-main",
                        "path": "src/example.py",
                        "purpose": "Primary batch change",
                    }
                ],
            },
            work_summary="Implemented the batch and ran focused verification.",
            verification=[
                {"command": "python -m pytest tests/unit/test_example.py -q", "result": "pass"}
            ],
        )
    elif role == "reviewer":
        payload = ReviewerResult(
            verdict="pass",
            scope="batch",
            target="W01",
            summary="Batch satisfies task requirements with no findings.",
            reviewed_target_ids=["src-main"],
        )
    else:
        raise ValueError(f"Unknown instruction role: {role}")
    return payload.model_dump(mode="json")


def format_result_json_schema(role: Role) -> str:
    model = result_model_for_role(role)
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True)


def format_result_example(role: Role) -> str:
    payload = canonical_result_payload(role)
    body = json.dumps(payload, indent=2, sort_keys=True)
    return f"{RESULT_BLOCK_START}\n{body}\n{RESULT_BLOCK_END}"
