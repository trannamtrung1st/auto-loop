"""Load an explicit v2 run manifest. No repository config discovery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from auto_loop.config import (
    AutoLoopConfig,
    ArtifactSettings,
    ConfigurationError,
    load_config,
)
from auto_loop.paths import (
    DEFAULT_ARTIFACTS_ROOT,
    PathContainmentError,
    assert_contained,
    canonical_artifacts_root_setting,
    configured_artifacts_root_rel,
    posix_rel,
    resolve_cli_path,
    resolve_workspace,
    resolve_workspace_path,
    workspace_relative,
)


@dataclass(frozen=True)
class RunLocator:
    path: Path
    workspace: Path
    artifact_root: Path
    artifacts_root_rel: str
    artifacts_root_setting: str


@dataclass(frozen=True)
class RunManifestSource:
    path: Path
    config: AutoLoopConfig
    workspace: Path
    artifact_root: Path
    task_source: Path
    artifacts_root_setting: str


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid {path.name}\n\nMalformed YAML: {exc}\n\nFix the file and run again."
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"Invalid {path.name}\n\nConfiguration root must be a mapping.\n\n"
            "Fix the field above and run again."
        )
    return raw


def _resolve_artifact_root(workspace: Path, artifacts_root: str) -> tuple[Path, str]:
    artifact_root = resolve_workspace_path(workspace, artifacts_root)
    try:
        artifact_root = assert_contained(workspace, artifact_root, label="artifacts.root")
    except PathContainmentError as exc:
        raise ConfigurationError(str(exc)) from exc
    rel = canonical_artifacts_root_setting(workspace, artifact_root)
    return artifact_root, rel


def load_run_locator(config_path: Path) -> RunLocator:
    """Parse only workspace and artifacts.root to locate an existing run."""
    path = resolve_cli_path(config_path)
    raw = _load_yaml_mapping(path)
    version = raw.get("version")
    if version == 1:
        from auto_loop.config import V1_UNSUPPORTED_MESSAGE

        raise ConfigurationError(V1_UNSUPPORTED_MESSAGE)
    if version != 2:
        raise ConfigurationError(
            f"Unsupported Auto Loop config version {version!r}.\n"
            "This release requires the single-manifest version 2 format."
        )
    workspace_raw = raw.get("workspace", ".")
    if not isinstance(workspace_raw, str):
        raise ConfigurationError(f"Invalid {path.name}\n\nworkspace must be a string.")
    try:
        workspace = resolve_workspace(path, workspace_raw)
    except OSError as exc:
        raise ConfigurationError(f"Could not resolve workspace: {exc}") from exc
    if not workspace.exists():
        raise ConfigurationError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise ConfigurationError(f"Workspace is not a directory: {workspace}")

    artifacts = raw.get("artifacts") if isinstance(raw.get("artifacts"), dict) else {}
    root_raw = artifacts.get("root", DEFAULT_ARTIFACTS_ROOT)
    if not isinstance(root_raw, str):
        raise ConfigurationError(f"Invalid {path.name}\n\nartifacts.root must be a string.")
    artifact_root, rel = _resolve_artifact_root(workspace, root_raw)
    return RunLocator(
        path=path,
        workspace=workspace,
        artifact_root=artifact_root,
        artifacts_root_rel=rel,
        artifacts_root_setting=root_raw,
    )


def with_canonical_artifacts(config: AutoLoopConfig, workspace: Path, artifact_root: Path) -> AutoLoopConfig:
    rel = canonical_artifacts_root_setting(workspace, artifact_root)
    if posix_rel(config.artifacts.root) == rel:
        return config
    return config.model_copy(update={"artifacts": ArtifactSettings(root=rel)})


def load_run_manifest(config_path: Path) -> RunManifestSource:
    """Parse the explicit YAML and resolve workspace, artifacts, and task source."""
    locator = load_run_locator(config_path)
    config = with_canonical_artifacts(load_config(locator.path), locator.workspace, locator.artifact_root)
    task_source = resolve_workspace_path(locator.workspace, config.task.source)
    return RunManifestSource(
        path=locator.path,
        config=config,
        workspace=locator.workspace,
        artifact_root=locator.artifact_root,
        task_source=task_source,
        artifacts_root_setting=locator.artifacts_root_setting,
    )


def resolve_operational_source(config_path: Path) -> RunManifestSource:
    """Locate a run using locator fields; prefer frozen config when a snapshot exists."""
    from auto_loop.config import load_resolved_config_optional

    locator = load_run_locator(config_path)
    frozen = load_resolved_config_optional(locator.artifact_root)
    if frozen is not None:
        config = with_canonical_artifacts(frozen, locator.workspace, locator.artifact_root)
        task_source = resolve_workspace_path(locator.workspace, config.task.source)
        return RunManifestSource(
            path=locator.path,
            config=config,
            workspace=locator.workspace,
            artifact_root=locator.artifact_root,
            task_source=task_source,
            artifacts_root_setting=locator.artifacts_root_setting,
        )
    return load_run_manifest(config_path)


def derive_protection(source: RunManifestSource) -> AutoLoopConfig:
    """Attach layout-derived protected files and product excludes to a frozen copy."""
    config = source.config
    protected = list(config.protection.protected_files)
    artifact_rel = posix_rel(config.artifacts_root)
    exclude_roots = [artifact_rel]
    configured_rel = configured_artifacts_root_rel(source.workspace, source.artifacts_root_setting)
    if configured_rel and configured_rel not in exclude_roots:
        exclude_roots.append(configured_rel)

    def add(rel: str | None) -> None:
        if rel and rel not in protected:
            protected.append(rel)

    add(workspace_relative(source.workspace, source.path))
    add(workspace_relative(source.workspace, source.task_source))
    add(f"{artifact_rel}/task.md")

    excludes = list(config.protection.product_exclude)
    for root in exclude_roots:
        for item in (f"{root}/", f"{root}/**"):
            if item not in excludes:
                excludes.append(item)

    protection = config.protection.model_copy(
        update={"protected_files": protected, "product_exclude": excludes}
    )
    return config.model_copy(update={"protection": protection})


def with_config(source: RunManifestSource, config: AutoLoopConfig) -> RunManifestSource:
    return replace(source, config=config)
