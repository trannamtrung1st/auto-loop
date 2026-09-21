"""Pre-run validation before provider invocation."""

from __future__ import annotations

from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import validate_context
from auto_loop.exits import ExitCode
from auto_loop.instructions import validate_custom_instruction_files


class RunPreconditionError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


def ensure_run_prerequisites(repo: Path, config: AutoLoopConfig) -> None:
    instruction_errors = validate_custom_instruction_files(repo, config)
    if instruction_errors:
        raise RunPreconditionError("; ".join(instruction_errors))

    context = validate_context(repo, config.context)
    if not context.ok_for_run:
        messages = [issue.message for issue in context.issues if issue.severity == "error"]
        raise RunPreconditionError("; ".join(messages) or "Invalid context resources")
