"""AUTO_LOOP_RESULT extraction and role result validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError

from auto_loop.models import (
    PROTOCOL_SCHEMA_VERSION,
    PlannerResult,
    ReviewerResult,
    WorkerResult,
)

RESULT_BLOCK_START = "<AUTO_LOOP_RESULT>"
RESULT_BLOCK_END = "</AUTO_LOOP_RESULT>"

Actor = Literal["planner", "worker", "reviewer"]
RoleResult = PlannerResult | WorkerResult | ReviewerResult


class ProtocolDiagnosticCode(StrEnum):
    MISSING_BLOCK = "missing_block"
    DUPLICATE_BLOCK = "duplicate_block"
    NESTED_BLOCK = "nested_block"
    MALFORMED_JSON = "malformed_json"
    EMPTY_BLOCK = "empty_block"
    WRONG_ACTOR = "wrong_actor"
    WORKER_FORBIDDEN_VERDICT = "worker_forbidden_verdict"
    SCHEMA_VIOLATION = "schema_violation"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


@dataclass(frozen=True)
class ProtocolDiagnostic:
    code: ProtocolDiagnosticCode
    message: str
    detail: str | None = None


class ProtocolParseError(Exception):
    """Terminal response did not yield a valid role handoff."""

    def __init__(self, diagnostic: ProtocolDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


def _find_block_spans(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, inner_json) for each non-nested result block."""
    spans: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        start = text.find(RESULT_BLOCK_START, pos)
        if start < 0:
            break
        content_start = start + len(RESULT_BLOCK_START)
        end_tag = text.find(RESULT_BLOCK_END, content_start)
        if end_tag < 0:
            break
        inner = text[content_start:end_tag]
        if RESULT_BLOCK_START in inner or RESULT_BLOCK_END in inner:
            raise ProtocolParseError(
                ProtocolDiagnostic(
                    code=ProtocolDiagnosticCode.NESTED_BLOCK,
                    message="Nested AUTO_LOOP_RESULT markup is not allowed",
                )
            )
        spans.append((start, end_tag + len(RESULT_BLOCK_END), inner.strip()))
        pos = end_tag + len(RESULT_BLOCK_END)
    return spans


def extract_result_json(text: str) -> str:
    """Extract JSON from exactly one AUTO_LOOP_RESULT block."""
    spans = _find_block_spans(text)
    if not spans:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.MISSING_BLOCK,
                message="No AUTO_LOOP_RESULT block found in terminal response",
            )
        )
    if len(spans) > 1:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.DUPLICATE_BLOCK,
                message=f"Expected one AUTO_LOOP_RESULT block, found {len(spans)}",
            )
        )
    payload = spans[0][2]
    if not payload:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.EMPTY_BLOCK,
                message="AUTO_LOOP_RESULT block is empty",
            )
        )
    return payload


def _load_json(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.MALFORMED_JSON,
                message="AUTO_LOOP_RESULT contains invalid JSON",
                detail=str(exc),
            )
        ) from exc
    if not isinstance(data, dict):
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.MALFORMED_JSON,
                message="AUTO_LOOP_RESULT JSON must be an object",
            )
        )
    return data


def _reject_implementer_forbidden_fields(data: dict[str, Any], actor: str) -> None:
    verdict = data.get("verdict")
    if verdict in ("complete", "pass"):
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.WORKER_FORBIDDEN_VERDICT,
                message=f"{actor.capitalize()} cannot emit reviewer verdict values",
                detail=f"verdict={verdict!r}",
            )
        )
    if data.get("status") in ("complete", "pass"):
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.WORKER_FORBIDDEN_VERDICT,
                message=f"{actor.capitalize()} status must be review_requested or blocked",
                detail=f"status={data.get('status')!r}",
            )
        )


def _require_schema_version(data: dict[str, Any], actor: str) -> None:
    if data.get("schema_version") != PROTOCOL_SCHEMA_VERSION:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.UNSUPPORTED_SCHEMA_VERSION,
                message=f"Unsupported {actor} schema_version",
                detail=str(data.get("schema_version")),
            )
        )


def _wrong_actor(expected: str, actual: Any) -> ProtocolParseError:
    return ProtocolParseError(
        ProtocolDiagnostic(
            code=ProtocolDiagnosticCode.WRONG_ACTOR,
            message=f"Expected {expected} AUTO_LOOP_RESULT",
            detail=f"actor={actual!r}",
        )
    )


def parse_planner_result(text: str) -> PlannerResult:
    data = _load_json(extract_result_json(text))
    actor = data.get("actor")
    if actor != "planner":
        raise _wrong_actor("planner", actor)
    _reject_implementer_forbidden_fields(data, "planner")
    _require_schema_version(data, "planner")
    try:
        return PlannerResult.model_validate(data)
    except ValidationError as exc:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
                message="Planner result failed schema validation",
                detail=str(exc),
            )
        ) from exc


def parse_worker_result(text: str) -> WorkerResult:
    data = _load_json(extract_result_json(text))
    actor = data.get("actor")
    if actor != "worker":
        raise _wrong_actor("worker", actor)
    _reject_implementer_forbidden_fields(data, "worker")
    _require_schema_version(data, "worker")
    try:
        return WorkerResult.model_validate(data)
    except ValidationError as exc:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
                message="Worker result failed schema validation",
                detail=str(exc),
            )
        ) from exc


def parse_reviewer_result(text: str) -> ReviewerResult:
    data = _load_json(extract_result_json(text))
    actor = data.get("actor")
    if actor != "reviewer":
        raise _wrong_actor("reviewer", actor)
    _require_schema_version(data, "reviewer")
    try:
        return ReviewerResult.model_validate(data)
    except ValidationError as exc:
        raise ProtocolParseError(
            ProtocolDiagnostic(
                code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
                message="Reviewer result failed schema validation",
                detail=str(exc),
            )
        ) from exc


def parse_role_result(text: str, expected: Actor) -> RoleResult:
    if expected == "planner":
        return parse_planner_result(text)
    if expected == "worker":
        return parse_worker_result(text)
    return parse_reviewer_result(text)


def missing_pass_targets(required_ids: list[str], reviewed_ids: list[str]) -> ProtocolParseError:
    missing = sorted(set(required_ids) - set(reviewed_ids))
    return ProtocolParseError(
        ProtocolDiagnostic(
            code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
            message=(
                "Approval requires reviewed_target_ids to include every active review target"
            ),
            detail=f"missing={missing}",
        )
    )


def normalize_commit(sha: str | None) -> str | None:
    if sha is None:
        return None
    cleaned = sha.strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", cleaned):
        return cleaned.lower()
    return cleaned
