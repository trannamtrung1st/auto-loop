"""Protocol extraction and validation tests."""

import json

import pytest

from auto_loop.models import PlannerResult, ReviewerResult, WorkerResult
from auto_loop.protocol import (
    ProtocolDiagnosticCode,
    ProtocolParseError,
    RESULT_BLOCK_END,
    RESULT_BLOCK_START,
    extract_result_blocks,
    extract_result_json,
    parse_planner_result,
    parse_reviewer_result,
    parse_role_result,
    parse_worker_result,
)
from auto_loop.reviews import render_review_markdown


def _wrap(payload: dict) -> str:
    return f"noise before\n{RESULT_BLOCK_START}\n{json.dumps(payload)}\n{RESULT_BLOCK_END}\ntrailing"


def _planner_payload(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "actor": "planner",
        "status": "review_requested",
        "review": {
            "scope": "plan",
            "target": "plan",
            "summary": "plan ready",
        },
        "plan_summary": "planned",
        "notes": [],
    }
    base.update(overrides)
    return base


def _worker_payload(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "plan",
            "target": "plan",
            "summary": "plan ready",
        },
        "work_summary": "wrote plan",
        "verification": [],
        "notes": [],
    }
    base.update(overrides)
    return base


def _reviewer_payload(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "actor": "reviewer",
        "verdict": "pass",
        "scope": "plan",
        "target": "plan",
        "summary": "looks good",
        "findings": [],
        "verification": [],
    }
    base.update(overrides)
    return base


def test_extract_result_blocks_finds_multiple_blocks():
    payload = json.dumps(_worker_payload())
    block = f"{RESULT_BLOCK_START}\n{payload}\n{RESULT_BLOCK_END}"
    text = f"Example (batch):\n{block}\n\nExample (final):\n{block}"
    blocks = extract_result_blocks(text)
    assert len(blocks) == 2
    assert blocks[0] == block
    assert blocks[1] == block


def test_extract_accepts_single_block_with_surrounding_prose():
    payload = _worker_payload()
    text = _wrap(payload)
    raw = extract_result_json(text)
    assert json.loads(raw)["actor"] == "worker"


def test_missing_block_fails():
    with pytest.raises(ProtocolParseError) as exc:
        extract_result_json("no block here")
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.MISSING_BLOCK


def test_duplicate_blocks_fail():
    payload = json.dumps(_worker_payload())
    text = f"{RESULT_BLOCK_START}{payload}{RESULT_BLOCK_END}\n{RESULT_BLOCK_START}{payload}{RESULT_BLOCK_END}"
    with pytest.raises(ProtocolParseError) as exc:
        extract_result_json(text)
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.DUPLICATE_BLOCK


def test_nested_markup_fails():
    text = f"{RESULT_BLOCK_START}{{ \"nested\": \"{RESULT_BLOCK_START}x{RESULT_BLOCK_END}\" }}{RESULT_BLOCK_END}"
    with pytest.raises(ProtocolParseError) as exc:
        extract_result_json(text)
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.NESTED_BLOCK


def test_malformed_json_fails():
    text = RESULT_BLOCK_START + "{not-json" + RESULT_BLOCK_END
    with pytest.raises(ProtocolParseError) as exc:
        parse_worker_result(text)
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.MALFORMED_JSON


def test_wrong_actor_rejected():
    with pytest.raises(ProtocolParseError) as exc:
        parse_worker_result(_wrap(_reviewer_payload()))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.WRONG_ACTOR


def test_worker_complete_verdict_rejected():
    bad = _worker_payload()
    bad["verdict"] = "complete"
    with pytest.raises(ProtocolParseError) as exc:
        parse_worker_result(_wrap(bad))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.WORKER_FORBIDDEN_VERDICT


def test_pass_with_findings_rejected():
    bad = _reviewer_payload(
        findings=[
            {
                "id": "F1",
                "title": "t",
                "detail": "d",
                "evidence": "e",
                "required_change": "c",
            }
        ]
    )
    with pytest.raises(ProtocolParseError) as exc:
        parse_reviewer_result(_wrap(bad))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.SCHEMA_VIOLATION


def test_revise_without_findings_rejected():
    bad = _reviewer_payload(verdict="revise")
    with pytest.raises(ProtocolParseError) as exc:
        parse_reviewer_result(_wrap(bad))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.SCHEMA_VIOLATION


def test_complete_requires_final_scope():
    bad = _reviewer_payload(verdict="complete", scope="batch", whole_task_reviewed=True)
    with pytest.raises(ProtocolParseError):
        parse_reviewer_result(_wrap(bad))


def test_parse_planner_result():
    result = parse_planner_result(_wrap(_planner_payload()))
    assert isinstance(result, PlannerResult)
    assert result.review is not None
    assert result.review.scope == "plan"


def test_planner_forbidden_batch_request():
    bad = _planner_payload()
    bad["review"]["scope"] = "batch"
    with pytest.raises(ProtocolParseError) as exc:
        parse_planner_result(_wrap(bad))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.SCHEMA_VIOLATION


def test_planner_forbidden_complete_verdict():
    bad = _planner_payload(status="complete")
    with pytest.raises(ProtocolParseError) as exc:
        parse_planner_result(_wrap(bad))
    assert exc.value.diagnostic.code == ProtocolDiagnosticCode.WORKER_FORBIDDEN_VERDICT


def test_parse_role_result_dispatch():
    planner = parse_role_result(_wrap(_planner_payload()), "planner")
    assert isinstance(planner, PlannerResult)
    worker = parse_role_result(_wrap(_worker_payload()), "worker")
    assert isinstance(worker, WorkerResult)
    reviewer = parse_role_result(_wrap(_reviewer_payload()), "reviewer")
    assert isinstance(reviewer, ReviewerResult)


def test_render_review_markdown_batch():
    worker = parse_worker_result(_wrap(_worker_payload(status="review_requested")))
    reviewer = parse_reviewer_result(
        _wrap(
            _reviewer_payload(
                scope="batch",
                reviewed_base_commit="abcdef0",
                reviewed_head_commit="1234567",
            )
        )
    )
    md = render_review_markdown(
        sequence=8,
        title="W01 revision",
        kind="revision",
        worker=worker,
        reviewer=reviewer,
        worker_session_id="worker-sess",
        reviewer_session_id="reviewer-sess",
    )
    assert "Review 0008" in md
    assert "abcdef0..1234567" in md
    assert "Worker summary" in md
    assert "Verdict: PASS" in md
