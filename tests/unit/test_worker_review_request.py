"""Worker ReviewRequest normalization (controller-owned Git fields)."""

from auto_loop.models import ReviewRequest


def test_legacy_git_range_target_stripped_from_worker_request():
    request = ReviewRequest.model_validate(
        {
            "scope": "batch",
            "target": "W06",
            "summary": "batch ready",
            "targets": [
                {
                    "kind": "git_range",
                    "id": "git",
                    "base_commit": "abc1234",
                    "head_commit": "def5678",
                }
            ],
            "base_commit": "abc1234",
            "head_commit": "def5678",
        }
    )
    assert request.legacy_git_range_ignored is True
    assert request.targets == []
    assert request.base_commit is None
    assert request.head_commit is None
