"""Load an explicit v2 run manifest. No repository config discovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from auto_loop.config import AutoLoopConfig, ConfigurationError, load_config
from auto_loop.paths import (
    PathContainmentError,
    assert_contained,
    posix_rel,
    resolve_cli_path,
    resolve_workspace,
    resolve_workspace_path,
    workspace_relative,
)


@dataclass(frozen=True)
class RunManifestSource:
    path: Path
    config: AutoLoopConfig
    workspace: Path
    artifact_root: Path
    task_source: Path


def load_run_manifest(config_path: Path) -> RunManifestSource:
    """Parse the explicit YAML and resolve workspace, artifacts, and task source."""
    path = resolve_cli_path(config_path)
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    config = load_config(path)
    try:
        workspace = resolve_workspace(path, config.workspace)
    except OSError as exc:
        raise ConfigurationError(f"Could not resolve workspace: {exc}") from exc
    if not workspace.exists():
        raise ConfigurationError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise ConfigurationError(f"Workspace is not a directory: {workspace}")

    artifact_root = resolve_workspace_path(workspace, config.artifacts.root)
    try:
        artifact_root = assert_contained(
            workspace, artifact_root, label="artifacts.root"
        )
    except PathContainmentError as exc:
        raise ConfigurationError(str(exc)) from exc

    task_source = resolve_workspace_path(workspace, config.task.source)
    return RunManifestSource(
        path=path,
        config=config,
        workspace=workspace,
        artifact_root=artifact_root,
        task_source=task_source,
    )


def locator_from_manifest(source: RunManifestSource) -> RunManifestSource:
    """Return the same locator; frozen config is loaded separately on resume."""
    return source


def derive_protection(source: RunManifestSource) -> AutoLoopConfig:
    """Attach layout-derived protected files and product excludes to a frozen copy."""
    config = source.config
    protected = list(config.protection.protected_files)
    artifact_rel = posix_rel(config.artifacts_root)

    def add(rel: str | None) -> None:
        if rel and rel not in protected:
            protected.append(rel)

    add(workspace_relative(source.workspace, source.path))
    add(workspace_relative(source.workspace, source.task_source))
    add(f"{artifact_rel}/task.md")

    excludes = list(config.protection.product_exclude)
    for item in (f"{artifact_rel}/", f"{artifact_rel}/**"):
        if item not in excludes:
            excludes.append(item)

    protection = config.protection.model_copy(
        update={"protected_files": protected, "product_exclude": excludes}
    )
    return config.model_copy(update={"protection": protection})


def with_config(source: RunManifestSource, config: AutoLoopConfig) -> RunManifestSource:
    return replace(source, config=config)
