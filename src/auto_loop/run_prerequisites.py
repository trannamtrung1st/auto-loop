"""Pre-run validation before provider invocation."""

from __future__ import annotations

from pathlib import Path

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import validate_context
from auto_loop.exits import ExitCode
from auto_loop.instructions import validate_custom_instruction_files
from auto_loop.manifest import RunManifestSource
from auto_loop.run_inputs import RunInputs, prepare_from_workspace_default, prepare_repo_for_run


class RunPreconditionError(Exception):
    exit_code = ExitCode.CONFIG_ERROR


def ensure_run_prerequisites(
    repo: Path,
    inputs: RunInputs | None = None,
    *,
    config: AutoLoopConfig | None = None,
    manifest: RunManifestSource | None = None,
) -> AutoLoopConfig:
    from auto_loop.run_inputs import RunInputError

    try:
        if config is None:
            if manifest is not None:
                resume = bool(inputs.resume_only) if inputs is not None else False
                prepared = prepare_repo_for_run(manifest, resume=resume)
                config = prepared.config
            else:
                prepared = prepare_from_workspace_default(repo, inputs)
                config = prepared.config
    except RunInputError as exc:
        raise RunPreconditionError(str(exc)) from exc

    instruction_errors = validate_custom_instruction_files(repo, config)
    if instruction_errors:
        raise RunPreconditionError("; ".join(instruction_errors))

    context = validate_context(repo, config.context)
    if not context.ok_for_run:
        messages = [issue.message for issue in context.issues if issue.severity == "error"]
        raise RunPreconditionError("; ".join(messages) or "Invalid context resources")

    return config
