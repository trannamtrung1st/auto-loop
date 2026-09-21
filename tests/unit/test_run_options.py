"""Run CLI option resolution tests."""

from auto_loop.config import default_config
from auto_loop.run_options import build_run_options


def test_run_options_override_config_limits():
    config = default_config()
    config.limits.max_turns = 10
    options = build_run_options(config, max_turns=3, worker_model="w-model")
    assert options.max_turns == 3
    assert options.worker_model == "w-model"
    assert options.reviewer_model == "auto"


def test_run_options_model_and_runtime_overrides():
    config = default_config()
    config.limits.max_runtime_minutes = 120
    options = build_run_options(
        config,
        model="shared",
        max_runtime_minutes=15,
        verbose=True,
        quiet=False,
    )
    assert options.worker_model == "shared"
    assert options.reviewer_model == "shared"
    assert options.max_runtime_minutes == 15
    assert options.verbose is True
