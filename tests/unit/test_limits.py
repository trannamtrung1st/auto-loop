"""Worker no-progress limit detection."""

from auto_loop.config import default_config
from auto_loop.lifecycle import create_lifecycle
from auto_loop.limits import update_worker_no_progress
def test_worker_no_progress_triggers_on_repeated_key():
    config = default_config()
    config.run.max_consecutive_worker_no_progress = 3
    state = create_lifecycle("abc123")
    key = "same-key"
    assert not update_worker_no_progress(state, config, key)
    assert not update_worker_no_progress(state, config, key)
    assert update_worker_no_progress(state, config, key)


def test_worker_no_progress_resets_on_change():
    config = default_config()
    state = create_lifecycle("abc123")
    update_worker_no_progress(state, config, "a")
    update_worker_no_progress(state, config, "a")
    assert not update_worker_no_progress(state, config, "b")
    assert state.worker_no_progress_streak == 1
