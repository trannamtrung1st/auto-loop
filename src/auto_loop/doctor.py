"""Workspace and provider diagnostics."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from auto_loop.config import AutoLoopConfig, ConfigurationError, load_config
from auto_loop.git import is_git_repository
from auto_loop.paths import auto_loop_root
from auto_loop.product_state import is_product_tree_clean
from auto_loop.lifecycle import session_consistency_errors
from auto_loop.providers.cursor import resolve_cursor_binary
from auto_loop.runtime import RuntimeStateError, load_lifecycle_state

DEFAULT_INSTRUCTION_PATHS = (
    ".auto-loop/instructions/shared.md",
    ".auto-loop/instructions/worker.md",
    ".auto-loop/instructions/reviewer.md",
)


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


def _path_readable(repo: Path, rel: str) -> bool:
    path = repo / rel
    return path.is_file() and os.access(path, os.R_OK)


def _check_workspace_layout(repo: Path, report: DoctorReport) -> AutoLoopConfig | None:
    root = auto_loop_root(repo)
    if not root.is_dir():
        report.add("workspace", Severity.ERROR, "Missing .auto-loop directory; run `auto-loop init`")
        return None

    config_path = root / "config.yaml"
    if not config_path.is_file():
        report.add("config", Severity.ERROR, f"Missing {config_path}")
        return None

    try:
        config = load_config(config_path)
    except ConfigurationError as exc:
        report.add("config", Severity.ERROR, f"Invalid configuration: {exc}")
        return None

    report.add("config", Severity.OK, "Configuration loaded and validated")
    return config


def _check_role_and_task_files(repo: Path, config: AutoLoopConfig, report: DoctorReport) -> None:
    for label, rel in (
        ("task", config.task_file),
        ("plan", config.plan_file),
        ("context", config.context_file),
        ("worker agent", config.agents["worker"].role_file),
        ("reviewer agent", config.agents["reviewer"].role_file),
    ):
        if _path_readable(repo, rel):
            report.add(f"file:{label}", Severity.OK, f"{rel} is readable")
        else:
            report.add(f"file:{label}", Severity.ERROR, f"Missing or unreadable {rel}")


def _check_instruction_templates(repo: Path, config: AutoLoopConfig, report: DoctorReport) -> None:
    configured = (
        list(config.instructions.shared.files)
        + list(config.instructions.worker.files)
        + list(config.instructions.reviewer.files)
    )
    if configured:
        for rel in configured:
            if _path_readable(repo, rel):
                report.add("instructions", Severity.OK, f"{rel} is readable")
            else:
                report.add(
                    "instructions",
                    Severity.ERROR,
                    f"Configured instruction file missing or unreadable: {rel}",
                )
        return

    missing = [rel for rel in DEFAULT_INSTRUCTION_PATHS if not _path_readable(repo, rel)]
    if missing:
        report.add(
            "instructions",
            Severity.ERROR,
            "Default instruction templates are missing (common after `auto-loop init --minimal`). "
            "Rerun `auto-loop init` without --minimal or create: "
            + ", ".join(missing),
        )
    else:
        report.add("instructions", Severity.OK, "Instruction templates present")


def _check_git(repo: Path, config: AutoLoopConfig, report: DoctorReport) -> None:
    if not config.git.require_repository:
        return
    if not is_git_repository(repo):
        report.add("git", Severity.ERROR, "Target path is not a Git repository")
        return
    report.add("git", Severity.OK, "Git repository detected")
    if config.git.require_clean_product_start and not is_product_tree_clean(repo):
        report.add(
            "git",
            Severity.WARNING,
            "Product working tree has uncommitted changes outside .auto-loop",
        )
    else:
        report.add("git", Severity.OK, "Product working tree is clean")


def _check_runtime_writable(repo: Path, report: DoctorReport) -> None:
    runtime = auto_loop_root(repo) / "runtime"
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


def _check_lifecycle_sessions(repo: Path, report: DoctorReport) -> None:
    try:
        state = load_lifecycle_state(repo)
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


def run_doctor(repo: Path, *, verbose: bool = False) -> DoctorReport:
    report = DoctorReport()
    config = _check_workspace_layout(repo, report)
    if config is None:
        return report
    _check_role_and_task_files(repo, config, report)
    _check_instruction_templates(repo, config, report)
    _check_git(repo, config, report)
    _check_runtime_writable(repo, report)
    _check_lifecycle_sessions(repo, report)
    _check_cursor_cli(config, report)
    return report
