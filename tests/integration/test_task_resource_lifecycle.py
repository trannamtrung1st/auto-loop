"""Fake-provider coverage for task-resource prompts and protection."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_loop.exits import ExitCode
from auto_loop.providers.scripted import ScriptedProvider
from tests.integration.scenario_harness import git, run_lifecycle, run_opts
from tests.repo_utils import git_repo


def _repo(tmp_path: Path) -> Path:
    repo = git_repo(tmp_path)
    (repo / ".ai").mkdir()
    (repo / ".ai" / "task.md").write_text(
        "# Task\n\nImplement the requirements in `proposal.md`.\n",
        encoding="utf-8",
    )
    (repo / "proposal.md").write_text("spec v1\n", encoding="utf-8")
    api = repo / "requirements" / "api.md"
    api.parent.mkdir(parents=True)
    api.write_text("api v1\n", encoding="utf-8")
    (repo / "README.md").write_text("Advisory overview\n", encoding="utf-8")
    (repo / ".ai" / "run.yaml").write_text(
        "version: 2\n"
        "workspace: ..\n"
        "task:\n"
        "  source: .ai/task.md\n"
        "  resources:\n"
        "    - proposal.md\n"
        "    - requirements/api.md\n"
        "artifacts:\n"
        "  root: .ai/auto-loop\n"
        "context:\n"
        "  shared:\n"
        "    resources:\n"
        "      - README.md\n"
        "git:\n"
        '  mode: "optional"\n'
        "  protect_approved_history: true\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "task inputs")
    return repo


class _MutatesResource(ScriptedProvider):
    def __init__(self, repo: Path, relative: str, role: str) -> None:
        super().__init__()
        self.repo = repo
        self.relative = relative
        self.role = role

    def invoke(self, argv: list[str]):
        if os.environ.get("AUTO_LOOP_FAKE_ROLE") == self.role:
            path = self.repo / self.relative
            path.write_text(path.read_text(encoding="utf-8") + "\nagent edit\n", encoding="utf-8")
        return super().invoke(argv)


def test_all_roles_see_frozen_task_resources(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = ScriptedProvider()
    provider.set_planner_review_request()
    provider.set_reviewer_pass("plan", "plan")
    provider.set_worker_blocked()
    provider.set_reviewer_blocked()
    outcome = run_lifecycle(repo, run_opts(4), provider)
    assert outcome.exit_code == ExitCode.BLOCKED
    prompts = {inv.role: inv.prompt for inv in provider.engine.invocations}
    for role in ("planner", "plan_reviewer", "worker", "reviewer"):
        text = prompts[role]
        assert "AUTHORITATIVE TASK RESOURCES" in text
        assert "Frozen snapshot: .ai/auto-loop/task-resources/proposal.md" in text
        assert "Frozen snapshot: .ai/auto-loop/task-resources/requirements/api.md" in text
        assert "task.md remains the highest-level authority." in text
        assert "AVAILABLE CONTEXT" in text
        assert "authoritative over task.md or frozen task resources" in text
        assert text.index("AUTHORITATIVE TASK RESOURCES") < text.index("AVAILABLE CONTEXT")
        assert "spec v1" not in text
        assert "api v1" not in text


@pytest.mark.parametrize(
    ("role", "turns"),
    [
        ("planner", 1),
        ("plan_reviewer", 2),
        ("worker", 3),
        ("reviewer", 4),
    ],
)
def test_role_cannot_modify_protected_task_resource(tmp_path: Path, role: str, turns: int):
    repo = _repo(tmp_path)
    provider = _MutatesResource(repo, "proposal.md", role)
    provider.set_planner_review_request()
    if turns >= 2:
        provider.set_reviewer_pass("plan", "plan")
    if turns >= 3:
        provider.set_worker_blocked()
    if turns >= 4:
        provider.set_reviewer_blocked()
    outcome = run_lifecycle(repo, run_opts(turns), provider)
    assert outcome.exit_code == ExitCode.PROTECTION_VIOLATION
    assert "agent edit" in (repo / "proposal.md").read_text(encoding="utf-8")


def test_planner_cannot_modify_frozen_task_resource_snapshot(tmp_path: Path):
    repo = _repo(tmp_path)
    provider = _MutatesResource(
        repo,
        ".ai/auto-loop/task-resources/proposal.md",
        "planner",
    )
    provider.set_planner_review_request()
    outcome = run_lifecycle(repo, run_opts(1), provider)
    assert outcome.exit_code == ExitCode.PROTECTION_VIOLATION
    text = (repo / ".ai" / "auto-loop" / "task-resources" / "proposal.md").read_text(encoding="utf-8")
    assert "agent edit" in text
    assert "spec v1" in text
