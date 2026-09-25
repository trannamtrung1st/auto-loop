"""AUTO_LOOP_RESULT JSON contract derived from protocol Pydantic models."""

from __future__ import annotations

import json
from typing import Any, Type

from pydantic import BaseModel

from auto_loop.models import PROTOCOL_SCHEMA_VERSION, PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import RESULT_BLOCK_END, RESULT_BLOCK_START

Role = str

_RESULT_MODEL_FOR_ROLE: dict[Role, Type[BaseModel]] = {
    "planner": PlannerResult,
    "worker": WorkerResult,
    "reviewer": ReviewerResult,
}

_RESULT_RULES: dict[Role, str] = {
    "planner": """\
- Every serialized result must include schema_version=2 and actor=planner.
- status=review_requested requires a non-null review with scope=plan.
- status=blocked may omit review; explain the blocker in plan_summary and notes.
- Never emit reviewer verdict fields or worker-style whole-task completion.""",
    "worker": """\
- Every serialized result must include schema_version=2 and actor=worker.
- `status` is a controller handoff action, not implementation progress. It has exactly two values:
  - `review_requested`: work/evidence is ready for review (even when more batches remain).
  - `blocked`: work cannot proceed because of a blocker.
- Never use progress states such as `implementing`, `in_progress`, `continued`, `done`, or `complete`.
- status=review_requested should include review describing scope, target, and evidence.
- Never emit reviewer verdict values (pass, revise, complete).""",
    "reviewer": """\
- Every serialized result must include schema_version=2 and actor=reviewer.
- verdict=pass or complete requires findings=[] (empty array).
- verdict=revise requires at least one finding with id, title, detail, evidence, required_change.
- verdict=complete requires scope=final and whole_task_reviewed=true (sole whole-task completion).
- PASS on scope=plan or batch does not complete the lifecycle.
- COMPLETE is valid only for scope=final by the execution reviewer.""",
}


def result_model_for_role(role: Role) -> Type[BaseModel]:
    try:
        return _RESULT_MODEL_FOR_ROLE[role]
    except KeyError:
        raise ValueError(f"Unknown instruction role: {role}") from None


def format_result_rules(role: Role) -> str:
    try:
        return _RESULT_RULES[role]
    except KeyError:
        raise ValueError(f"Unknown instruction role: {role}") from None


def _planner_example() -> PlannerResult:
    return PlannerResult(
        schema_version=PROTOCOL_SCHEMA_VERSION,
        actor="planner",
        status="review_requested",
        review={
            "scope": "plan",
            "target": "plan",
            "summary": "Plan ready for independent review.",
        },
        plan_summary="Outlined approach, scope, and verification strategy.",
    )


def _worker_example() -> WorkerResult:
    return WorkerResult(
        schema_version=PROTOCOL_SCHEMA_VERSION,
        actor="worker",
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


def _reviewer_batch_pass_example() -> ReviewerResult:
    return ReviewerResult(
        schema_version=PROTOCOL_SCHEMA_VERSION,
        actor="reviewer",
        verdict="pass",
        scope="batch",
        target="W01",
        summary="Batch satisfies task requirements with no findings.",
        reviewed_target_ids=["src-main"],
    )


def _reviewer_final_complete_example() -> ReviewerResult:
    return ReviewerResult(
        schema_version=PROTOCOL_SCHEMA_VERSION,
        actor="reviewer",
        verdict="complete",
        scope="final",
        target="final",
        summary="Whole task reviewed; all requirements satisfied.",
        whole_task_reviewed=True,
        reviewed_target_ids=["git"],
    )


def canonical_result_payload(role: Role) -> dict[str, Any]:
    """Primary example object for the role; validated against the Pydantic model."""
    if role == "planner":
        payload = _planner_example()
    elif role == "worker":
        payload = _worker_example()
    elif role == "reviewer":
        payload = _reviewer_batch_pass_example()
    else:
        raise ValueError(f"Unknown instruction role: {role}")
    return payload.model_dump(mode="json")


def _wrap_result_block(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, sort_keys=True)
    return f"{RESULT_BLOCK_START}\n{body}\n{RESULT_BLOCK_END}"


def format_result_json_schema(role: Role) -> str:
    model = result_model_for_role(role)
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True)


def format_result_example(role: Role) -> str:
    if role == "reviewer":
        return "\n\n".join(
            [
                "Example (batch PASS):",
                _wrap_result_block(_reviewer_batch_pass_example().model_dump(mode="json")),
                "Example (final COMPLETE; requires whole_task_reviewed=true):",
                _wrap_result_block(_reviewer_final_complete_example().model_dump(mode="json")),
            ]
        )
    return _wrap_result_block(canonical_result_payload(role))


def format_output_repair_protocol_contract(role: Role) -> str:
    """Compact role contract for output-only protocol repair (rules + example block)."""
    return "\n".join(
        [
            "Required envelope for this turn (adapt values to your prior work; "
            "do not invent new implementation):",
            "",
            format_result_rules(role),
            "",
            "Canonical example:",
            format_result_example(role),
        ]
    )
