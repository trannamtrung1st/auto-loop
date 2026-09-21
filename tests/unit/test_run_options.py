"""Run CLI option resolution tests."""

from auto_loop.config import default_config
from auto_loop.run_options import build_run_options


def test_run_options_override_config_limits():
    config = default_config()
    config.run.max_turns = 10
    options = build_run_options(config, max_turns=3, worker_model="w-model")
    assert options.max_turns == 3
    assert options.worker_model == "w-model"
    assert options.reviewer_model == "auto"


def test_run_options_model_and_runtime_overrides():
    config = default_config()
    config.run.max_runtime_minutes = 120
    options = build_run_options(
        config,
        model="shared",
        max_runtime_minutes=15,
        verbose=True,
        quiet=False,
    )
    assert options.worker_model == "shared"
    assert options.reviewer_model == "shared"
    assert options.planner_model == "shared"
    assert options.max_runtime_minutes == 15
    assert options.verbose is True
    assert options.console_level == "verbose"


def test_run_options_uses_config_role_models():
    config = default_config()
    config.agents["planner"].model = "p-cfg"
    config.agents["worker"].model = "w-cfg"
    config.agents["reviewer"].model = "r-cfg"
    options = build_run_options(config)
    assert options.planner_model == "p-cfg"
    assert options.worker_model == "w-cfg"
    assert options.reviewer_model == "r-cfg"


def test_run_options_role_cli_beats_global_and_config():
    config = default_config()
    config.agents["worker"].model = "w-cfg"
    options = build_run_options(
        config,
        model="shared",
        planner_model="p-cli",
        worker_model="w-cli",
        reviewer_model="r-cli",
    )
    assert options.planner_model == "p-cli"
    assert options.worker_model == "w-cli"
    assert options.reviewer_model == "r-cli"


def test_run_options_quiet_overrides_config_console():
    config = default_config()
    config.logging.console = "verbose"
    options = build_run_options(config, quiet=True)
    assert options.console_level == "quiet"
