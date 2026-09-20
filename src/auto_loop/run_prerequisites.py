"""Pre-run validation before provider invocation."""

from __future__ import annotations

from pathlib import Path

from auto_loop.config import AutoLoopConfig, load_config_from_repo
from auto_loop.context_manifest import validate_context_file
from auto_loop.doctor import DEFAULT_INSTRUCTION_PATHS
from auto_loop.exits import ExitCode
from auto_loop.instructions import validate_custom_instruction_files


class RunPreconditionError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


def ensure_run_prerequisites(repo: Path) -> AutoLoopConfig:
    config = load_config_from_repo(repo)
    instruction_errors = validate_custom_instruction_files(repo, config)
    if instruction_errors:
        raise RunPreconditionError("; ".join(instruction_errors))

    if not config.instructions.worker.files:
        missing = [
            rel
            for rel in DEFAULT_INSTRUCTION_PATHS
            if not (repo / rel).is_file()
        ]
        if missing:
            raise RunPreconditionError(
                "Missing instruction templates required for run "
                f"({', '.join(missing)}). Rerun `auto-loop init` without --minimal "
                "or create the files manually."
            )

    context = validate_context_file(repo, repo / config.context_file)
    if not context.ok_for_run:
        messages = [issue.message for issue in context.issues if issue.severity == "error"]
        raise RunPreconditionError("; ".join(messages) or "Invalid context.yaml")

    return config
