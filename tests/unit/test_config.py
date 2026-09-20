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
    parse_config_dict,
)


def test_default_config_round_trip():
    cfg = default_config()
    assert cfg.version == 1
    assert cfg.provider.type == "cursor"
    assert cfg.provider.cursor.worker_extra_args == ["--force"]
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


def test_load_config_from_file(tmp_path: Path):
    cfg = default_config()
    path = tmp_path / "config.yaml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.task_file == cfg.task_file


def test_load_config_missing_file(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_load_config_malformed_yaml(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(":\n  bad:\n- ", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Malformed YAML"):
        load_config(path)
