"""Shared helpers for proposal section 44 integration scenarios."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

from auto_loop.git import head_commit
from auto_loop.init_cmd import bootstrap_workspace
from auto_loop.loop import run_lifecycle as _core_run_lifecycle
from auto_loop.manifest import load_run_manifest, resolve_operational_source
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.run_inputs import (
    has_active_lifecycle,
    has_lifecycle_state,
    has_suspended_blocked_record,
    has_terminal_record,
    prepare_repo_for_run,
)
from auto_loop.run_options import RunOptions
from auto_loop.config import AutoLoopConfig, load_resolved_config_optional


def bootstrapped_manifest_path(repo: Path) -> Path:
    return repo / ".ai" / "run.yaml"


def run_lifecycle(
    repo: Path,
    options: RunOptions,
    provider: ScriptedProvider,
    *,
    config: AutoLoopConfig | None = None,
    artifact_root: Path | None = None,
):
    """Integration helper: prepare from the bootstrapped manifest unless config is supplied."""
    if config is not None:
        from auto_loop.paths import resolved_artifact_root

        root = artifact_root or resolved_artifact_root(repo, config.artifacts_root)
        return _core_run_lifecycle(repo, options, provider, config=config, artifact_root=root)

    manifest_path = bootstrapped_manifest_path(repo)
    operational = resolve_operational_source(manifest_path)
    if has_suspended_blocked_record(operational.workspace, operational.artifact_root) and not options.resuming:
        frozen = load_resolved_config_optional(operational.artifact_root) or operational.config
        return _core_run_lifecycle(
            operational.workspace,
            options,
            provider,
            config=frozen,
            artifact_root=operational.artifact_root,
        )
    if has_active_lifecycle(operational.workspace, operational.artifact_root):
        prepared = prepare_repo_for_run(operational, resume=True)
    elif not has_lifecycle_state(operational.workspace, operational.artifact_root):
        prepared = prepare_repo_for_run(load_run_manifest(manifest_path), resume=False)
    elif has_terminal_record(operational.workspace, operational.artifact_root):
        return _core_run_lifecycle(
            operational.workspace,
            options,
            provider,
            config=operational.config,
            artifact_root=operational.artifact_root,
        )
    else:
        prepared = prepare_repo_for_run(operational, resume=True)
    effective = options
    if prepared.is_resume and not options.resuming:
        effective = replace(options, resuming=True)
    return _core_run_lifecycle(
        prepared.workspace,
        effective,
        provider,
        config=prepared.config,
        artifact_root=prepared.artifact_root,
    )


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "commit", "--allow-empty", "-m", "init")
    bootstrap_workspace(repo)
    return repo


def run_opts(max_turns: int = 10, *, resuming: bool = False) -> RunOptions:
    return RunOptions(
        "auto",
        "auto",
        max_turns=max_turns,
        max_runtime_minutes=60,
        verbose=False,
        quiet=True,
        resuming=resuming,
    )


def batch_worker_payload(target: str = "W01") -> dict:
    return {
        "schema_version": 2,
        "actor": "worker",
        "status": "review_requested",
        "review": {
            "scope": "batch",
            "target": target,
            "summary": "batch",
        },
        "work_summary": "implemented",
        "verification": [],
        "notes": [],
    }


def batch_worker_payload_with_path(
    path: str,
    *,
    target: str = "W01",
    path_id: str = "extra",
) -> dict:
    """Batch request with an explicit path target (Git range is controller-owned)."""
    payload = batch_worker_payload(target=target)
    payload["review"]["targets"] = [
        {
            "kind": "path",
            "id": path_id,
            "path": path,
            "purpose": "explicit review target",
        }
    ]
    return payload


def approve_plan(repo: Path, provider: ScriptedProvider) -> None:
    provider.set_worker_plan_request()
    provider.set_reviewer_pass("plan", "plan")
    run_lifecycle(repo, run_opts(2), provider)


def commit_file(repo: Path, rel: str, content: str, message: str) -> str:
    path = repo / rel
    path.write_text(content, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "commit", "-m", message)
    return head_commit(repo)


def latest_review_text(repo: Path) -> str:
    reviews = sorted((repo / ".ai" / "auto-loop" / "reviews").glob("*.md"))
    assert reviews, "expected a review artifact"
    return reviews[-1].read_text(encoding="utf-8")


def execution_reviewer_invocation_count(provider: ScriptedProvider) -> int:
    return sum(1 for inv in provider.engine.invocations if inv.role == "reviewer")


def reviewer_invocation_count(provider: ScriptedProvider) -> int:
    return sum(
        1
        for inv in provider.engine.invocations
        if inv.role in ("reviewer", "plan_reviewer")
    )


def worker_invocation_count(provider: ScriptedProvider) -> int:
    return sum(1 for inv in provider.engine.invocations if inv.role == "worker")


def worker_session_ids(provider: ScriptedProvider) -> list[str]:
    return [inv.resume_session_id or "" for inv in provider.engine.invocations if inv.role == "worker"]


def reviewer_session_ids(provider: ScriptedProvider) -> list[str]:
    return [
        inv.resume_session_id or ""
        for inv in provider.engine.invocations
        if inv.role == "reviewer"
    ]


class PromptCapturingProvider:
    """Records worker and reviewer prompts from fake-agent argv."""

    def __init__(self) -> None:
        self._inner = ScriptedProvider()
        self.worker_prompts: list[str] = []
        self.reviewer_prompts: list[str] = []

    @property
    def engine(self):
        return self._inner.engine

    def prepare(self, role: str) -> None:
        self._inner.prepare(role)

    def invoke(self, argv: list[str]):
        if len(argv) > 1 and argv[0] == "fake-agent":
            prompt = argv[-1]
            fake_role = os.environ.get("AUTO_LOOP_FAKE_ROLE")
            if fake_role in ("reviewer", "plan_reviewer"):
                self.reviewer_prompts.append(prompt)
            elif fake_role in ("worker", "planner"):
                self.worker_prompts.append(prompt)
        return self._inner.invoke(argv)

    def set_response(self, role: str, payload: dict) -> None:
        self._inner.set_response(role, payload)

    def set_worker_plan_request(self) -> None:
        self._inner.set_worker_plan_request()

    def set_planner_review_request(self) -> None:
        self._inner.set_planner_review_request()

    def set_plan_reviewer_pass(self, target: str = "plan") -> None:
        self._inner.set_plan_reviewer_pass(target)

    def set_reviewer_pass(self, scope: str, target: str) -> None:
        self._inner.set_reviewer_pass(scope, target)

    def set_reviewer_revise(self, scope: str, target: str, finding_id: str = "f-1") -> None:
        self._inner.set_reviewer_revise(scope, target, finding_id)

    def set_worker_final_request(self, head: str | None = None) -> None:
        self._inner.set_worker_final_request(head=head)

    def set_reviewer_complete(
        self,
        head: str | None = None,
        *,
        target_ids: list[str] | None = None,
    ) -> None:
        self._inner.set_reviewer_complete(head, target_ids=target_ids)

    def set_worker_blocked(self, summary: str = "blocked on external dependency") -> None:
        self._inner.set_worker_blocked(summary)

    def set_reviewer_blocked(self, summary: str = "external intervention required") -> None:
        self._inner.set_reviewer_blocked(summary)

    def set_invalid_protocol_response(self, role: str) -> None:
        self._inner.set_invalid_protocol_response(role)
