"""Environment-gated real Cursor smoke test helpers (proposal section 45)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from auto_loop.doctor import run_doctor
from auto_loop.exits import ExitCode
from auto_loop.git import head_commit
from auto_loop.init_cmd import run_init
from auto_loop.providers.cursor import resolve_cursor_binary
from auto_loop.runtime import load_lifecycle_state
from auto_loop.terminal_records import load_completion_record

LIVE_OPT_IN_ENV = "AUTO_LOOP_LIVE_CURSOR"

SMOKE_TASK = """# Smoke task

Add a minimal Python package under `src/`:

- Create `src/greet.py` with a function `greet(name: str) -> str` returning `f"Hello, {name}!"`.
- Add `tests/test_greet.py` with one pytest that asserts `greet("world") == "Hello, world!"`.
- Commit product changes on the worker branch before each batch review request.

Follow the auto-loop worker protocol: plan review first, then batch review for implementation, then final whole-task review.
"""

SMOKE_PLAN = """# Plan

1. Request plan review with scope `plan`.
2. Implement `src/greet.py` and `tests/test_greet.py` in one batch (`W01`).
3. Request final whole-task review when tests pass and HEAD matches last approved commit.
"""


@dataclass(frozen=True)
class LiveSmokeGate:
    enabled: bool
    skip_reason: str


def live_smoke_gate() -> LiveSmokeGate:
    if os.environ.get(LIVE_OPT_IN_ENV) != "1":
        return LiveSmokeGate(
            False,
            f"Set {LIVE_OPT_IN_ENV}=1 to opt into the real Cursor smoke test.",
        )
    try:
        from auto_loop.config import default_config

        resolve_cursor_binary(default_config().provider.cursor)
    except FileNotFoundError as exc:
        return LiveSmokeGate(False, str(exc))
    return LiveSmokeGate(True, "")


def prepare_smoke_repository(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "smoke@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Smoke"], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "smoke"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init smoke project"], cwd=repo, check=True, capture_output=True)
    run_init(repo)
    (repo / ".auto-loop" / "task.md").write_text(SMOKE_TASK, encoding="utf-8")
    (repo / ".auto-loop" / "plan.md").write_text(SMOKE_PLAN, encoding="utf-8")


@dataclass
class SmokeEvidence:
    worker_session_id: str | None
    reviewer_session_id: str | None
    plan_review_before_product_commits: bool
    completion_matches_head: bool
    review_count: int
    exit_code: int
    doctor_ok: bool
    notes: list[str]


def collect_smoke_evidence(repo: Path, exit_code: ExitCode) -> SmokeEvidence:
    notes: list[str] = []
    state = load_lifecycle_state(repo)
    record = load_completion_record(repo)
    reviews = sorted((repo / ".auto-loop" / "reviews").glob("*.md"))
    worker_id = state.sessions["worker"].session_id if state else None
    reviewer_id = state.sessions["reviewer"].session_id if state else None
    if worker_id and reviewer_id and worker_id == reviewer_id:
        notes.append("worker and reviewer session ids must differ")
    plan_before_code = bool(state and state.plan_approved)
    if state and not reviews:
        notes.append("missing review artifacts")
    completion_ok = False
    if record is not None:
        completion_ok = record.final_commit == head_commit(repo)
    elif int(exit_code) == int(ExitCode.COMPLETE):
        notes.append("COMPLETE exit but no completion.json")
    doctor_ok = run_doctor(repo).ok
    return SmokeEvidence(
        worker_session_id=worker_id,
        reviewer_session_id=reviewer_id,
        plan_review_before_product_commits=plan_before_code,
        completion_matches_head=completion_ok,
        review_count=len(reviews),
        exit_code=int(exit_code),
        doctor_ok=doctor_ok,
        notes=notes,
    )


def assert_smoke_success(evidence: SmokeEvidence) -> None:
    errors: list[str] = []
    if evidence.exit_code != int(ExitCode.COMPLETE):
        errors.append(f"expected exit COMPLETE, got {evidence.exit_code}")
    if not evidence.worker_session_id or not evidence.reviewer_session_id:
        errors.append("missing persistent worker or reviewer session id")
    if evidence.worker_session_id == evidence.reviewer_session_id:
        errors.append("session ids must be distinct")
    if not evidence.plan_review_before_product_commits:
        errors.append("plan review before implementation not evidenced")
    if evidence.review_count < 2:
        errors.append("expected plan and batch/final review artifacts")
    if not evidence.completion_matches_head:
        errors.append("completion.json final_commit must match HEAD")
    if not evidence.doctor_ok:
        errors.append("doctor checks failed after smoke run")
    errors.extend(evidence.notes)
    if errors:
        raise AssertionError("; ".join(errors))
