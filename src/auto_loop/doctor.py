"""Workspace and provider diagnostics for an explicit run manifest."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from auto_loop.config import AutoLoopConfig, ConfigurationError
from auto_loop.git_policy import git_policy_issues
from auto_loop.context_manifest import validate_context
from auto_loop.instructions import validate_custom_instruction_files
from auto_loop.lifecycle import session_consistency_errors
from auto_loop.manifest import RunManifestSource
from auto_loop.providers.cursor import resolve_cursor_binary
from auto_loop.locking import describe_lock_status
from auto_loop.runtime import RuntimeStateError, load_lifecycle_state
from auto_loop.stop_control import reconcile_stale_runtime
from auto_loop.run_inputs import validate_task_source


class Severity(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class DoctorCheck:
    check_id: str
    severity: Severity
    message: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    def add(self, check_id: str, severity: Severity, message: str) -> None:
        self.checks.append(DoctorCheck(check_id, severity, message))

    @property
    def ok(self) -> bool:
        return not any(check.severity == Severity.ERROR for check in self.checks)

    def render(self, verbose: bool = False) -> str:
        lines: list[str] = []
        for check in self.checks:
            if check.severity == Severity.OK and not verbose:
                continue
            prefix = check.severity.value.upper()
            lines.append(f"[{prefix}] {check.check_id}: {check.message}")
        if not lines and self.ok:
            lines.append("All doctor checks passed.")
        return "\n".join(lines)


def _path_readable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.R_OK)


def _artifact_ignored(workspace: Path, artifact_root: Path) -> bool:
    gitignore = workspace / ".gitignore"
    if not gitignore.is_file():
        return False
    try:
        rel = artifact_root.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True
    rel_text = str(rel).replace("\\", "/")
    prefixes = {rel_text, f"{rel_text}/", f"{rel_text}/**", str(rel_text).split("/")[0] + "/"}
    existing = {line.strip().rstrip("/") for line in gitignore.read_text(encoding="utf-8").splitlines()}
    for item in existing:
        cleaned = item.rstrip("/")
        if cleaned in prefixes or rel_text.startswith(cleaned.rstrip("*").rstrip("/") + "/"):
            return True
        if item in {rel_text, f"{rel_text}/", f"{rel_text}/**"}:
            return True
    return False


def _check_manifest(source: RunManifestSource, report: DoctorReport) -> AutoLoopConfig:
    report.add("config", Severity.OK, f"Run config loaded: {source.path}")
    report.add("workspace", Severity.OK, f"Workspace: {source.workspace}")
    report.add(
        "artifacts",
        Severity.OK,
        f"Artifact root is contained in workspace: {source.artifact_root}",
    )
    if not _artifact_ignored(source.workspace, source.artifact_root):
        report.add(
            "artifacts:gitignore",
            Severity.WARNING,
            "Artifact root is inside the repository and is not listed in .gitignore",
        )
    return source.config


def _check_task_source(source: RunManifestSource, report: DoctorReport) -> None:
    try:
        validate_task_source(source)
    except Exception as exc:
        report.add("task", Severity.ERROR, str(exc))
        return
    report.add("task", Severity.OK, f"Task source is readable: {source.task_source}")


def _check_role_and_runtime_files(
    source: RunManifestSource,
    config: AutoLoopConfig,
    report: DoctorReport,
    *,
    require_task_snapshot: bool,
) -> None:
    task_snapshot = source.artifact_root / "task.md"
    plan = source.artifact_root / "plan.md"
    if _path_readable(task_snapshot):
        report.add("file:task-snapshot", Severity.OK, f"{config.task_file} is readable")
    elif require_task_snapshot:
        report.add("file:task-snapshot", Severity.ERROR, f"Missing task snapshot {config.task_file}")
    else:
        report.add(
            "file:task-snapshot",
            Severity.WARNING,
            "No task snapshot yet; it is created when a run starts",
        )
    if _path_readable(plan):
        report.add("file:plan", Severity.OK, f"{config.plan_file} is readable")
    elif source.artifact_root.is_dir():
        report.add("file:plan", Severity.WARNING, f"No plan file yet at {config.plan_file}")
    for role in ("planner", "worker", "reviewer"):
        role_file = config.agents[role].role_file
        if role_file:
            if _path_readable(source.workspace / role_file):
                report.add(f"file:{role} agent", Severity.OK, f"{role_file} is readable")
            else:
                report.add(
                    f"file:{role} agent",
                    Severity.ERROR,
                    f"Configured role file missing or unreadable: {role_file}",
                )


def _check_instruction_composition(
    repo: Path, config: AutoLoopConfig, report: DoctorReport
) -> None:
    errors = validate_custom_instruction_files(repo, config)
    if errors:
        for message in errors:
            report.add("instructions:composition", Severity.ERROR, message)
    else:
        report.add("instructions:composition", Severity.OK, "Instruction composition paths are valid")


def _check_git(repo: Path, config: AutoLoopConfig, report: DoctorReport) -> None:
    for issue in git_policy_issues(repo, config):
        severity = {
            "ok": Severity.OK,
            "warning": Severity.WARNING,
            "error": Severity.ERROR,
        }[issue.severity]
        report.add("git", severity, issue.message)


def _check_runtime_writable(artifact_root: Path, report: DoctorReport) -> None:
    runtime = artifact_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    probe = runtime / ".doctor-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add("runtime", Severity.OK, f"{runtime} is writable")
    except OSError as exc:
        report.add("runtime", Severity.ERROR, f"Cannot write to {runtime}: {exc}")


def _check_cursor_cli(config: AutoLoopConfig, report: DoctorReport) -> None:
    try:
        binary = resolve_cursor_binary(config.provider.cursor)
    except FileNotFoundError as exc:
        report.add("cursor", Severity.ERROR, str(exc))
        return
    report.add("cursor", Severity.OK, f"Cursor CLI resolved: {binary}")

    try:
        version = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("cursor", Severity.WARNING, f"Could not query Cursor CLI version: {exc}")
        return

    if version.returncode != 0:
        report.add(
            "cursor",
            Severity.WARNING,
            "Cursor CLI `--version` failed; ensure `agent login` completed and CLI is functional",
        )
    else:
        first_line = (version.stdout or version.stderr or "").strip().splitlines()[:1]
        detail = first_line[0] if first_line else "unknown version"
        report.add("cursor", Severity.OK, f"Cursor CLI version: {detail}")

    help_result = subprocess.run(
        [binary, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    help_text = (help_result.stdout or "") + (help_result.stderr or "")
    for flag, label in (
        ("--resume", "explicit resume"),
        ("stream-json", "stream-json output"),
        ("ask", "ask mode"),
    ):
        if flag in help_text:
            report.add(f"cursor:{label}", Severity.OK, f"Cursor help mentions {flag}")
        else:
            report.add(
                f"cursor:{label}",
                Severity.WARNING,
                f"Cursor help does not mention {flag}; verify installed CLI capabilities",
            )


def _check_context_manifest(repo: Path, config: AutoLoopConfig, report: DoctorReport) -> None:
    result = validate_context(repo, config.context)
    for issue in result.issues:
        severity = Severity.ERROR if issue.severity == "error" else Severity.WARNING
        report.add("context", severity, issue.message)
    if result.ok_for_run:
        report.add("context", Severity.OK, "Context resource paths validated")


def _check_stale_runtime(repo: Path, artifact_root: Path, report: DoctorReport) -> None:
    """Reclaim a dead controller's provider, lock, and active-run record."""
    try:
        result = reconcile_stale_runtime(repo, artifact_root)
    except OSError as exc:
        report.add("runtime", Severity.ERROR, f"Could not reconcile runtime ownership: {exc}")
        return
    if result.remote_ownership or result.unverified_ownership:
        report.add("runtime", Severity.WARNING, result.message)
        return
    if result.changed:
        report.add("runtime", Severity.WARNING, result.message)
        return
    report.add("runtime", Severity.OK, result.message)


def _check_workspace_lock(repo: Path, artifact_root: Path, report: DoctorReport) -> None:
    severity, message = describe_lock_status(repo, artifact_root)
    if severity == "error":
        report.add("lock", Severity.ERROR, message)
    elif severity == "warning":
        report.add("lock", Severity.WARNING, message)
    else:
        report.add("lock", Severity.OK, message)


def _check_lifecycle_sessions(repo: Path, artifact_root: Path, report: DoctorReport) -> None:
    try:
        state = load_lifecycle_state(repo, artifact_root)
    except RuntimeStateError as exc:
        report.add("lifecycle", Severity.ERROR, str(exc))
        return
    if state is None:
        report.add("lifecycle", Severity.OK, "No active lifecycle state file")
        return
    errors = session_consistency_errors(state)
    if errors:
        for message in errors:
            report.add("lifecycle", Severity.ERROR, message)
    else:
        report.add("lifecycle", Severity.OK, "Stored session metadata is consistent")


def run_doctor(source: RunManifestSource, *, verbose: bool = False) -> DoctorReport:
    del verbose
    report = DoctorReport()
    try:
        config = _check_manifest(source, report)
    except ConfigurationError as exc:
        report.add("config", Severity.ERROR, str(exc))
        return report
    _check_task_source(source, report)
    _check_git(source.workspace, config, report)
    _check_cursor_cli(config, report)
    _check_context_manifest(source.workspace, config, report)
    _check_instruction_composition(source.workspace, config, report)
    state = None
    try:
        state = load_lifecycle_state(source.workspace, source.artifact_root)
    except RuntimeStateError:
        state = None
    _check_role_and_runtime_files(
        source, config, report, require_task_snapshot=state is not None
    )
    _check_runtime_writable(source.artifact_root, report)
    _check_stale_runtime(source.workspace, source.artifact_root, report)
    _check_workspace_lock(source.workspace, source.artifact_root, report)
    _check_lifecycle_sessions(source.workspace, source.artifact_root, report)
    return report
