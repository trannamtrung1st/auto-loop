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
    load_config_from_repo,
    load_resolved_config_from_repo,
    overlay_user_config,
    parse_config_dict,
    write_resolved_config,
)


def test_default_config_round_trip():
    cfg = default_config()
    assert cfg.version == 1
    assert cfg.provider.type == "cursor"
    assert cfg.provider.cursor.worker_extra_args == ["--force"]
    assert cfg.agents["planner"].mode == "agent"
    assert cfg.agents["planner"].role_file.endswith("planner.md")
    assert cfg.agents["worker"].mode == "agent"
    assert cfg.agents["reviewer"].mode == "ask"
    assert cfg.limits.max_turns == 100
    reloaded = AutoLoopConfig.model_validate(yaml.safe_load(dump_config(cfg)))
    assert reloaded == cfg


def test_unknown_config_version_rejected():
    with pytest.raises(ConfigurationError, match="Unsupported config version"):
        parse_config_dict({"version": 99})


def test_invalid_instruction_mode_rejected():
    data = default_config().model_dump(mode="json")
    data["instructions"]["worker"]["mode"] = "replace_all"
    with pytest.raises(ConfigurationError):
        parse_config_dict(data)


def test_impossible_limits_rejected():
    data = default_config().model_dump(mode="json")
    data["limits"]["max_turns"] = 0
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
    path = tmp_path / "config.yaml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.task_file == cfg.task_file


def test_protected_files_merge_defaults_into_existing_list():
    data = default_config().model_dump(mode="json")
    data["protection"]["protected_files"] = [".auto-loop/task.md"]
    cfg = parse_config_dict(data)
    assert ".auto-loop/task.md" in cfg.protection.protected_files
    assert any("planner" in p for p in cfg.protection.protected_files)


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_malformed_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(":\n  bad:\n- ", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_config(path)


def test_user_config_overlay_models_and_run(tmp_path: Path):
    path = tmp_path / "auto-loop.yaml"
    path.write_text(
        "models:\n  worker: gpt-5.6\nrun:\n  max_turns: 7\n",
        encoding="utf-8",
    )
    cfg = overlay_user_config(default_config(), path)
    assert cfg.agents["worker"].model == "gpt-5.6"
    assert cfg.agents["planner"].model == "auto"
    assert cfg.limits.max_turns == 7


def test_load_config_from_repo_prefers_user_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auto-loop.yaml").write_text(
        "models:\n  planner: p-user\n",
        encoding="utf-8",
    )
    cfg = load_config_from_repo(repo)
    assert cfg.agents["planner"].model == "p-user"


def test_invalid_user_config_names_the_file(tmp_path: Path):
    path = tmp_path / "auto-loop.yaml"
    path.write_text("run:\n  mystery: 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Invalid auto-loop.yaml"):
        overlay_user_config(default_config(), path)


def test_resume_uses_frozen_snapshot_not_changed_user_yaml(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "auto-loop.yaml").write_text(
        "run:\n  max_turns: 11\n",
        encoding="utf-8",
    )
    base = default_config()
    base.limits.max_turns = 11
    write_resolved_config(repo, base)
    (repo / "auto-loop.yaml").write_text(
        "run:\n  max_turns: 99\n",
        encoding="utf-8",
    )
    resolved = load_resolved_config_from_repo(repo)
    assert resolved.limits.max_turns == 11
    merged = load_config_from_repo(repo)
    assert merged.limits.max_turns == 99
