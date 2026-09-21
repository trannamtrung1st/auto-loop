"""Environment-gated real Cursor smoke test helpers (proposal section 45)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from auto_loop.config import load_config_from_repo
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

Follow the auto-loop protocol: the planner requests plan review first, then the worker implements a batch, then the execution reviewer performs final whole-task review.
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


def _review_field(text: str, label: str) -> str | None:
    prefix = f"- {label}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.split(":", 1)[1].strip()
            if value.startswith("`") and value.endswith("`"):
                value = value[1:-1]
            return value
    return None


def _session_ids_from_reviews(reviews: list[Path]) -> tuple[set[str], set[str]]:
    workers: set[str] = set()
    reviewers: set[str] = set()
    for path in reviews:
        text = path.read_text(encoding="utf-8")
        worker = _review_field(text, "Worker session")
        reviewer = _review_field(text, "Reviewer session")
        if worker:
            workers.add(worker)
        if reviewer:
            reviewers.add(reviewer)
    return workers, reviewers


def _product_commit_count(repo: Path, base: str) -> int:
    result = subprocess.run(
        [
            "git",
            "rev-list",
            "--count",
            f"{base}..HEAD",
            "--",
            ".",
            ":(exclude).auto-loop",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or "0")


def section_45_check_notes(repo: Path) -> list[str]:
    """Return human-readable failures for proposal §45 verify bullets."""
    notes: list[str] = []
    state = load_lifecycle_state(repo)
    if state is None:
        return ["missing lifecycle state"]
    config = load_config_from_repo(repo)
    from auto_loop.events import load_events

    events = load_events(repo, config)
    reviews = sorted((repo / ".auto-loop" / "reviews").glob("*.md"))
    worker_ids: set[str] = set()
    reviewer_ids: set[str] = set()
    for path in reviews:
        text = path.read_text(encoding="utf-8")
        purpose = _review_field(text, "Reviewer session purpose")
        worker = _review_field(text, "Worker session")
        reviewer = _review_field(text, "Reviewer session")
        if purpose == "plan_reviewer":
            continue
        if worker:
            worker_ids.add(worker)
        if reviewer:
            reviewer_ids.add(reviewer)

    if len(worker_ids) > 1:
        notes.append("worker session id changed across review artifacts")
    if len(reviewer_ids) > 1:
        notes.append("reviewer session id changed across review artifacts")
    if state.sessions["worker"].session_id and worker_ids and state.sessions["worker"].session_id not in worker_ids:
        notes.append("stored worker session id does not match review artifacts")
    if state.sessions["reviewer"].session_id and reviewer_ids and state.sessions["reviewer"].session_id not in reviewer_ids:
        notes.append("stored reviewer session id does not match review artifacts")

    created = [e for e in events if e.get("type") == "session_created"]
    if len(created) < 2:
        notes.append("expected session_created events for worker and reviewer")
    actors = {e.get("actor") for e in created}
    if not {"worker", "reviewer"} <= actors:
        notes.append("session_created events must cover worker and reviewer roles")

    plan_pass_idx = None
    first_baseline_idx = None
    for idx, event in enumerate(events):
        if event.get("type") == "review_result" and event.get("scope") == "plan" and event.get("verdict") == "pass":
            plan_pass_idx = idx
        if event.get("type") == "baseline_advanced" and first_baseline_idx is None:
            first_baseline_idx = idx
    if plan_pass_idx is None:
        notes.append("no plan review PASS recorded in events")
    elif first_baseline_idx is not None and first_baseline_idx < plan_pass_idx:
        notes.append("baseline advanced before plan PASS")
    if _product_commit_count(repo, state.initial_base_commit) < 1:
        notes.append("worker did not commit product changes after initial baseline")

    for idx, event in enumerate(events):
        if event.get("type") != "baseline_advanced":
            continue
        prior = None
        for j in range(idx - 1, -1, -1):
            if events[j].get("type") == "review_result":
                prior = events[j]
                break
        if prior is None or prior.get("scope") != "batch" or prior.get("verdict") != "pass":
            notes.append("baseline_advanced without preceding batch PASS review_result")

    batch_reviews = []
    final_reviews = []
    for path in reviews:
        text = path.read_text(encoding="utf-8")
        scope = _review_field(text, "Scope")
        verdict = _review_field(text, "Verdict")
        if scope == "batch":
            batch_reviews.append((path, text, verdict))
        if scope == "final":
            whole = _review_field(text, "Whole task reviewed")
            final_reviews.append((verdict, whole))

    has_baseline_advance = any(e.get("type") == "baseline_advanced" for e in events)
    if not batch_reviews:
        notes.append("missing batch review artifact")
    elif not any(v == "PASS" for _, _, v in batch_reviews):
        notes.append("missing batch PASS review artifact")
    else:
        ranged_batch = False
        for _, text, verdict in batch_reviews:
            if verdict != "PASS":
                continue
            base = _review_field(text, "Approved baseline")
            head = _review_field(text, "Candidate HEAD")
            range_line = _review_field(text, "Reviewed range")
            if base and head:
                ranged_batch = True
                if range_line and range_line != f"{base}..{head}":
                    notes.append("batch review range line does not match baseline..HEAD")
        if not ranged_batch and not has_baseline_advance:
            notes.append("batch PASS without documented range or baseline_advanced event")

    if not any(v == "COMPLETE" and w and w.lower() == "yes" for v, w in final_reviews):
        notes.append("missing final whole-task COMPLETE review with whole_task_reviewed")

    record = load_completion_record(repo)
    if record is None:
        notes.append("missing completion.json")
    elif record.final_commit != head_commit(repo):
        notes.append("completion.json final_commit must match HEAD")

    return notes


@dataclass
class SmokeEvidence:
    worker_session_id: str | None
    reviewer_session_id: str | None
    plan_review_before_product_commits: bool
    completion_matches_head: bool
    review_count: int
    exit_code: int
    doctor_ok: bool
    notes: list[str] = field(default_factory=list)


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
    notes.extend(section_45_check_notes(repo))
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
