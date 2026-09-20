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
