"""Optional starter run-manifest generator. Not required to start a run."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from auto_loop.config import write_resolved_config
from auto_loop.exits import ExitCode
from auto_loop.manifest import RunManifestSource, load_run_manifest
from auto_loop.paths import posix_rel, resolve_cli_path, workspace_relative


class InitError(Exception):
    """Initialization refused or failed."""

    exit_code = ExitCode.CONFIG_ERROR


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    message: str = ""


def _read_template(relative: str) -> str:
    package = resources.files("auto_loop").joinpath("templates")
    return package.joinpath(relative).read_text(encoding="utf-8")


def _starter_manifest_text(*, workspace: str, task_source: str, artifacts_root: str) -> str:
    return (
        "# Auto Loop run manifest (version 2).\n"
        "# Pass this file to the CLI: auto-loop run PATH/TO/THIS.yaml\n"
        "#\n"
        "# workspace is resolved relative to this file's directory.\n"
        "# All other paths are resolved relative to workspace.\n"
        "\n"
        "version: 2\n"
        f"workspace: {workspace}\n"
        "\n"
        "task:\n"
        f"  source: {task_source}\n"
        "\n"
        "artifacts:\n"
        f"  root: {artifacts_root}\n"
        "\n"
        "models:\n"
        "  planner: auto\n"
        "  worker: auto\n"
        "  reviewer: auto\n"
        "\n"
        "run:\n"
        "  max_turns: 100\n"
        "  max_runtime_minutes: 480\n"
    )


def _workspace_rel_from_yaml(yaml_path: Path) -> str:
    yaml_dir = yaml_path.parent.resolve()
    cwd = Path.cwd().resolve()
    try:
        rel = cwd.relative_to(yaml_dir)
        text = posix_rel(str(rel))
        return text if text else "."
    except ValueError:
        import os

        return posix_rel(os.path.relpath(cwd, yaml_dir))


def run_init(target: Path, *, force: bool = False) -> InitResult:
    """Create a starter v2 run YAML at `target`. Does not create runtime state or a task."""
    path = resolve_cli_path(target)
    if path.exists() and path.is_dir():
        path = path / "run.yaml"
    result = InitResult()
    if path.exists() and not force:
        result.skipped.append(str(path))
        result.message = (
            f"Run config already exists: {path}\n\n"
            "Pass --force to overwrite. This command does not create runtime state."
        )
        return result

    workspace_rel = _workspace_rel_from_yaml(path)
    workspace = (path.parent / workspace_rel).resolve() if workspace_rel != "." else path.parent.resolve()
    if workspace_rel == ".":
        workspace = path.parent.resolve()

    proposal = path.parent / "proposal.md"
    task_rel = workspace_relative(workspace, proposal) or "proposal.md"
    artifact_dir = path.parent / "auto-loop"
    artifact_rel = workspace_relative(workspace, artifact_dir) or ".ai/auto-loop"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _starter_manifest_text(
            workspace=workspace_rel,
            task_source=task_rel,
            artifacts_root=artifact_rel,
        ),
        encoding="utf-8",
    )
    result.created.append(str(path))
    result.message = (
        "Created Auto Loop run config.\n"
        "\n"
        f"Config: {path}\n"
        f"Suggested task file: {proposal}\n"
        "\n"
        "Next:\n"
        f"  auto-loop doctor {path}\n"
        f"  auto-loop run {path}\n"
        "\n"
        "This command did not create a task file or runtime state."
    )
    return result


def materialize_artifact_layout(source: RunManifestSource, *, plan_text: str | None = None) -> None:
    """Create artifact directories lazily. Does not write agent or instruction files."""
    root = source.artifact_root
    (root / "reviews").mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    plan_path = root / "plan.md"
    if not plan_path.exists():
        plan_path.write_text(plan_text or _read_template("plan.md"), encoding="utf-8")


def reset_run_scoped_workspace(source: RunManifestSource) -> None:
    """Regenerate the plan for a new lifecycle. Preserves runtime archives."""
    plan_path = source.artifact_root / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(_read_template("plan.md"), encoding="utf-8")
    (source.artifact_root / "reviews").mkdir(parents=True, exist_ok=True)
    (source.artifact_root / "runtime").mkdir(parents=True, exist_ok=True)


def bootstrap_workspace(
    repo: Path,
    *,
    force: bool = False,
    minimal: bool = False,
    goal: str = "Test task",
) -> InitResult:
    """Create a v2 manifest, proposal, and frozen run snapshot. Intended for tests."""
    del force, minimal
    ai_dir = repo / ".ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    proposal = ai_dir / "proposal.md"
    if not proposal.is_file() or not proposal.read_text(encoding="utf-8").strip():
        text = goal if goal.endswith("\n") else f"{goal}\n"
        proposal.write_text(text, encoding="utf-8")
    manifest_path = ai_dir / "run.yaml"
    manifest_path.write_text(
        _starter_manifest_text(
            workspace="..",
            task_source=".ai/proposal.md",
            artifacts_root=".ai/auto-loop",
        ),
        encoding="utf-8",
    )
    from auto_loop.run_inputs import prepare_repo_for_run

    prepared = prepare_repo_for_run(load_run_manifest(manifest_path), resume=False)
    write_resolved_config(prepared.workspace, prepared.config)
    _commit_user_owned_inputs(repo)
    return InitResult(created=[".ai/run.yaml", ".ai/proposal.md"], message="bootstrapped")


def _commit_user_owned_inputs(repo: Path) -> None:
    """Commit the user-owned manifest and task so tests start with a clean product tree."""
    if not (repo / ".git").exists():
        return
    subprocess.run(
        ["git", "add", "--", ".ai/run.yaml", ".ai/proposal.md"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo, capture_output=True)
    if staged.returncode == 0:
        return
    subprocess.run(
        ["git", "commit", "-m", "Add Auto Loop run inputs"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
