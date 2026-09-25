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


_REPAIR_FOOTER = "Re-emit the result only. Do not redo successful work."


def _compact_validation_detail(detail: str) -> str:
    """Turn Pydantic validation traces into short field-scoped lines."""
    text = detail.strip()
    if not text:
        return text
    if "validation error" not in text.splitlines()[0]:
        return text
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("For further information"):
            index += 1
            continue
        if "validation error" in line:
            index += 1
            continue
        if index + 1 < len(lines) and lines[index + 1].startswith("  "):
            field = line
            msg_line = lines[index + 1].strip()
            msg = msg_line.split("[", 1)[0].strip()
            received = ""
            marker = "input_value="
            if marker in msg_line:
                start = msg_line.index(marker) + len(marker)
                if msg_line[start] == "'":
                    end = msg_line.find("'", start + 1)
                    if end > start:
                        received = f"; received {msg_line[start + 1 : end]!r}."
                elif msg_line[start] == '"':
                    end = msg_line.find('"', start + 1)
                    if end > start:
                        received = f"; received {msg_line[start + 1 : end]!r}."
            out.append(f"{field}: {msg}{received}")
            index += 2
            continue
        out.append(line)
        index += 1
    return "\n".join(out) if out else text


def format_protocol_repair_reason(exc: ProtocolParseError) -> str:
    """Canonical repair text for prompts, durable state, and CLI diagnostics."""
    diag = exc.diagnostic
    parts = [diag.message]
    if diag.detail:
        compact = _compact_validation_detail(diag.detail)
        if compact and compact not in parts:
            parts.append(compact)
    parts.append(_REPAIR_FOOTER)
    return "\n".join(parts)


def protocol_repair_diagnostic_lines(repair_reason: str) -> list[str]:
    """User-facing parse/validation lines from a stored repair reason."""
    lines: list[str] = []
    for part in repair_reason.splitlines():
        stripped = part.strip()
        if not stripped or stripped == _REPAIR_FOOTER:
            continue
        if stripped.startswith("Re-emit the result only"):
            continue
        lines.append(stripped)
    return lines


def _line_is_marker(line: str, marker: str) -> bool:
    return line.strip() == marker


def _find_block_spans(text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, inner_json) for each line-delimited result block."""
    if not text:
        return []
    line_entries: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        line_entries.append((offset, end, line))
        offset = end

    spans: list[tuple[int, int, str]] = []
    index = 0
    while index < len(line_entries):
        start_offset, start_end, start_line = line_entries[index]
        if not _line_is_marker(start_line, RESULT_BLOCK_START):
            index += 1
            continue
        inner_lines: list[str] = []
        scan = index + 1
        while scan < len(line_entries):
            _, _, candidate = line_entries[scan]
            if _line_is_marker(candidate, RESULT_BLOCK_END):
                end_offset, end_end, _ = line_entries[scan]
                inner = "".join(inner_lines).strip()
                if any(
                    _line_is_marker(part, RESULT_BLOCK_START)
                    or _line_is_marker(part, RESULT_BLOCK_END)
                    for part in inner.splitlines()
                ):
                    raise ProtocolParseError(
                        ProtocolDiagnostic(
                            code=ProtocolDiagnosticCode.NESTED_BLOCK,
                            message="Nested AUTO_LOOP_RESULT markup is not allowed",
                        )
                    )
                spans.append((start_offset, end_end, inner))
                index = scan + 1
                break
            if _line_is_marker(candidate, RESULT_BLOCK_START):
                raise ProtocolParseError(
                    ProtocolDiagnostic(
                        code=ProtocolDiagnosticCode.NESTED_BLOCK,
                        message="Nested AUTO_LOOP_RESULT markup is not allowed",
                    )
                )
            inner_lines.append(candidate)
            scan += 1
        else:
            break
    return spans


def extract_result_blocks(text: str) -> list[str]:
    """Return each full AUTO_LOOP_RESULT block in document order."""
    spans = _find_block_spans(text)
    return [text[start:end] for start, end, _ in spans]


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


def plan_reviewer_forbidden_complete() -> ProtocolParseError:
    return ProtocolParseError(
        ProtocolDiagnostic(
            code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
            message="Plan reviewer cannot declare task completion (verdict=complete)",
            detail=(
                "Re-emit a planning review result with verdict pass, revise, or blocked only."
            ),
        )
    )


def reviewer_active_binding_mismatch(field: str, expected: str, actual: str) -> ProtocolParseError:
    return ProtocolParseError(
        ProtocolDiagnostic(
            code=ProtocolDiagnosticCode.SCHEMA_VIOLATION,
            message=(
                f"Reviewer result {field} must exactly match the active review request "
                f"(expected {expected!r}, got {actual!r})"
            ),
            detail=(
                "Re-emit the same review verdict, findings, and verification with corrected "
                "protocol metadata (scope, target, and Git commit fields when applicable)."
            ),
        )
    )


def normalize_commit(sha: str | None) -> str | None:
    if sha is None:
        return None
    cleaned = sha.strip()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", cleaned):
        return cleaned.lower()
    return cleaned
