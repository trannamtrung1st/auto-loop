"""Configuration model and load tests."""

from pathlib import Path

import pytest
import yaml

from auto_loop.config import (
    AutoLoopConfig,
    ConfigurationError,
    default_config,
    dump_config,
    load_config,
    load_resolved_config_optional,
    parse_config_dict,
    write_resolved_config,
)
from auto_loop.manifest import load_run_manifest
from auto_loop.paths import DEFAULT_ARTIFACTS_ROOT


def test_default_config_round_trip():
    cfg = default_config()
    assert cfg.version == 2
    assert cfg.provider.type == "cursor"
    assert cfg.provider.cursor.worker_extra_args == ["--force"]
    assert cfg.agents["planner"].mode == "agent"
    assert cfg.agents["planner"].role_file == ""
    assert cfg.agents["worker"].mode == "agent"
    assert cfg.agents["reviewer"].mode == "ask"
    assert cfg.limits.max_turns == 100
    assert cfg.task_file == f"{DEFAULT_ARTIFACTS_ROOT}/task.md"
    reloaded = AutoLoopConfig.model_validate(yaml.safe_load(dump_config(cfg)))
    assert reloaded.version == cfg.version
    assert reloaded.task.source == cfg.task.source
    assert reloaded.agents["reviewer"].mode == "ask"
    assert "limits" not in yaml.safe_load(dump_config(cfg))


def test_unknown_config_version_rejected():
    with pytest.raises(ConfigurationError, match="Unsupported Auto Loop config version"):
        parse_config_dict({"version": 99, "workspace": ".", "task": {"source": "t.md"}})


def test_v1_config_rejected_with_clear_message():
    with pytest.raises(ConfigurationError, match="version 1"):
        parse_config_dict({"version": 1, "models": {"worker": "auto"}})


def test_invalid_instruction_mode_rejected():
    data = default_config().model_dump(mode="json")
    data["instructions"]["worker"]["mode"] = "replace_all"
    with pytest.raises(ConfigurationError):
        parse_config_dict(data)


def test_impossible_limits_rejected():
    data = default_config().model_dump(mode="json")
    data["run"]["max_turns"] = 0
    with pytest.raises(ConfigurationError):
        parse_config_dict(data)


def test_reviewer_must_use_ask_mode():
    data = default_config().model_dump(mode="json")
    data["agents"]["reviewer"]["mode"] = "agent"
    with pytest.raises(ConfigurationError, match="ask mode"):
        parse_config_dict(data)


def test_config_fills_missing_planner_agent():
    data = default_config().model_dump(mode="json")
    data["agents"].pop("planner")
    cfg = parse_config_dict(data)
    assert "planner" in cfg.agents
    assert cfg.agents["planner"].mode == "agent"
    assert cfg.agents["planner"].model == "auto"


def test_load_config_from_file(tmp_path: Path):
    cfg = default_config()
    path = tmp_path / "run.yaml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.task_file == cfg.task_file


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_malformed_yaml(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(":\n  bad:\n- ", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_config(path)


def test_models_and_run_apply_to_agents_and_limits(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: proposal.md\n"
        "models:\n  worker: gpt-5.6\nrun:\n  max_turns: 7\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.agents["worker"].model == "gpt-5.6"
    assert cfg.agents["planner"].model == "auto"
    assert cfg.limits.max_turns == 7


def test_run_rejects_invalid_limits(tmp_path: Path):
    for body in (
        "version: 2\nworkspace: .\ntask:\n  source: t.md\nrun:\n  max_turns: 0\n",
        "version: 2\nworkspace: .\ntask:\n  source: t.md\nrun:\n  max_turns: -3\n",
        "version: 2\nworkspace: .\ntask:\n  source: t.md\nrun:\n  max_runtime_minutes: 0\n",
    ):
        path = tmp_path / "run.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_config(path)


def test_resume_uses_frozen_snapshot_not_changed_source_yaml(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "proposal.md").write_text("goal\n", encoding="utf-8")
    yaml_path = repo / "run.yaml"
    yaml_path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: proposal.md\nrun:\n  max_turns: 11\n",
        encoding="utf-8",
    )
    source = load_run_manifest(yaml_path)
    cfg = source.config
    write_resolved_config(repo, cfg)
    yaml_path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: proposal.md\nrun:\n  max_turns: 99\n",
        encoding="utf-8",
    )
    frozen = load_resolved_config_optional(source.artifact_root)
    assert frozen is not None
    assert frozen.limits.max_turns == 11
    reloaded = load_run_manifest(yaml_path)
    assert reloaded.config.limits.max_turns == 99


def test_context_string_resources_parse(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "version: 2\nworkspace: .\ntask:\n  source: proposal.md\n"
        "context:\n  shared:\n    resources:\n      - README.md\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.context.shared.resources[0].path == "README.md"
    assert cfg.context.shared.resources[0].purpose == "README.md"
