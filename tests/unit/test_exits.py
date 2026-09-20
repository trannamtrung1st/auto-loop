"""Exit code map tests."""

from auto_loop.exits import EXIT_CODE_NAMES, ExitCode


def test_exit_code_values_match_proposal():
    assert int(ExitCode.COMPLETE) == 0
    assert int(ExitCode.BLOCKED) == 2
    assert int(ExitCode.LIMIT_REACHED) == 3
    assert int(ExitCode.STOPPED) == 4
    assert int(ExitCode.CONFIG_ERROR) == 10
    assert int(ExitCode.PROVIDER_ERROR) == 11
    assert int(ExitCode.PROTOCOL_ERROR) == 12
    assert int(ExitCode.PROTECTION_VIOLATION) == 13
    assert int(ExitCode.CONCURRENT_RUN) == 14
    assert int(ExitCode.GIT_PROTOCOL_ERROR) == 15
    assert int(ExitCode.SESSION_ERROR) == 16
    assert int(ExitCode.REVIEW_MUTATION_ERROR) == 17
    assert int(ExitCode.INTERNAL_ERROR) == 18


def test_exit_code_names_cover_all_codes():
    for code in ExitCode:
        assert int(code) in EXIT_CODE_NAMES
        assert EXIT_CODE_NAMES[int(code)] == code.name
