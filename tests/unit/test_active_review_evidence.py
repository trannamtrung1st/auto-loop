"""Unit tests for active review evidence validation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from auto_loop.active_review_evidence import (
    STALE_REVIEW_REASON,
    active_review_evidence_matches,
    assess_active_review_evidence_mismatch,
)
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.lifecycle import ActiveReview
from auto_loop.review_targets import plan_path_target, sha256_file
from tests.repo_utils import frozen_config


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    bootstrap_workspace(repo, minimal=True)
    return repo


def test_final_candidate_must_match_head(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    active = ActiveReview(
        cycle_id="c1",
        scope="final",
        target="whole-task",
        summary="s",
        current_candidate_head=head,
        approved_base_commit=head,
    )
    assert active_review_evidence_matches(repo, config, active)
    path = repo / "feature.txt"
    path.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True)
    mismatch = assess_active_review_evidence_mismatch(repo, config, active)
    assert mismatch is not None
    assert mismatch.reason == STALE_REVIEW_REASON
    assert mismatch.expected_head == head


def test_plan_hash_mismatch(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    plan_path = repo / config.plan_file
    plan_hash = sha256_file(plan_path)
    active = ActiveReview(
        cycle_id="c1",
        scope="plan",
        target="plan",
        summary="s",
        plan_sha256=plan_hash,
        targets=[plan_path_target(repo, config.plan_file, git_mode=config.git.mode)],
    )
    assert active_review_evidence_matches(repo, config, active)
    plan_path.write_text("# changed\n", encoding="utf-8")
    assert not active_review_evidence_matches(repo, config, active)


def test_required_mode_dirty_tree_invalidates_matching_head(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    path = repo / "feature.txt"
    path.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    active = ActiveReview(
        cycle_id="c1",
        scope="batch",
        target="W01",
        summary="s",
        current_candidate_head=head,
        approved_base_commit=head,
    )
    assert active_review_evidence_matches(repo, config, active)
    path.write_text("uncommitted change\n", encoding="utf-8")
    mismatch = assess_active_review_evidence_mismatch(repo, config, active)
    assert mismatch is not None
    assert any("product working tree is not clean" in item for item in mismatch.details)
    assert mismatch.observed_head == head


def test_plan_review_allows_dirty_product_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    plan_path = repo / config.plan_file
    plan_path.write_text("# plan\n", encoding="utf-8")
    plan_hash = sha256_file(plan_path)
    (repo / "partial.txt").write_text("partial\n", encoding="utf-8")
    active = ActiveReview(
        cycle_id="c1",
        scope="plan",
        target="plan",
        summary="execution replan",
        session_purpose="plan_reviewer",
        plan_sha256=plan_hash,
        targets=[plan_path_target(repo, config.plan_file, git_mode=config.git.mode)],
    )
    assert active_review_evidence_matches(repo, config, active)


def test_waiting_adjudication_allows_dirty_product_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    path = repo / "feature.txt"
    path.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    active = ActiveReview(
        cycle_id="c1",
        scope="batch",
        target="waiting",
        summary="wait",
        current_candidate_head=head,
    )
    path.write_text("uncommitted change\n", encoding="utf-8")
    assert active_review_evidence_matches(repo, config, active)


def test_blocked_adjudication_allows_dirty_product_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    config = frozen_config(repo)
    path = repo / "feature.txt"
    path.write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    active = ActiveReview(
        cycle_id="c1",
        scope="batch",
        target="blocked",
        summary="blocked",
        current_candidate_head=head,
    )
    path.write_text("uncommitted change\n", encoding="utf-8")
    assert active_review_evidence_matches(repo, config, active)
