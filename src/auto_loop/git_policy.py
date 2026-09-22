"""Shared Git-mode policy for run prerequisites and doctor."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from auto_loop.config import AutoLoopConfig, GitMode
from auto_loop.git import is_git_repository
from auto_loop.product_state import is_product_tree_clean, product_excludes

SeverityName = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class GitPolicyIssue:
    severity: SeverityName
    message: str


def git_mode(config: AutoLoopConfig) -> GitMode:
    return config.git.mode


def git_commands_allowed(config: AutoLoopConfig) -> bool:
    return config.git.mode != "off"


def strict_git(config: AutoLoopConfig) -> bool:
    return config.git.mode == "required"


def git_usable(repo: Path, config: AutoLoopConfig) -> bool:
    """True when this run may inspect Git state.

    ``mode=off`` does not invoke Git, even if a repository happens to exist.
    """
    if not git_commands_allowed(config):
        return False
    return is_git_repository(repo)


def git_policy_issues(repo: Path, config: AutoLoopConfig) -> list[GitPolicyIssue]:
    """Conditions doctor reports and run enforces at the matching checkpoint.

    A dirty tree is not a startup failure: strict mode rejects it when planning
    ends or a review is requested, which is also when doctor flags it.
    """
    mode = config.git.mode
    if mode == "off":
        return [
            GitPolicyIssue(
                "ok",
                "Git mode is off; review uses explicit targets and a repository is not required",
            )
        ]
    if not is_git_repository(repo):
        if mode == "required":
            return [
                GitPolicyIssue(
                    "error",
                    "git.mode=required requires a Git repository",
                )
            ]
        return [
            GitPolicyIssue(
                "ok",
                "No Git repository; optional mode reviews explicit path and content targets",
            )
        ]
    issues = [GitPolicyIssue("ok", "Git repository detected")]
    excludes = product_excludes(config)
    dirty = not is_product_tree_clean(repo, excludes=excludes)
    if dirty and mode == "required":
        issues.append(
            GitPolicyIssue(
                "error",
                "git.mode=required rejects a dirty product tree at planning close and review",
            )
        )
    elif dirty:
        issues.append(
            GitPolicyIssue(
                "warning",
                "Product working tree has uncommitted changes; commits are optional review evidence",
            )
        )
    else:
        issues.append(GitPolicyIssue("ok", "Product working tree is clean"))
    return issues


def repository_required_error(repo: Path, config: AutoLoopConfig) -> str | None:
    """Startup failure when strict mode has no repository. Dirty trees are separate."""
    for issue in git_policy_issues(repo, config):
        if issue.severity == "error" and "Git repository" in issue.message:
            return issue.message
    return None
