"""Stable CLI exit codes (proposal section 36)."""

from enum import IntEnum


class ExitCode(IntEnum):
    COMPLETE = 0
    BLOCKED = 2
    LIMIT_REACHED = 3
    STOPPED = 4

    CONFIG_ERROR = 10
    PROVIDER_ERROR = 11
    PROTOCOL_ERROR = 12
    PROTECTION_VIOLATION = 13
    CONCURRENT_RUN = 14
    GIT_PROTOCOL_ERROR = 15
    SESSION_ERROR = 16
    REVIEW_MUTATION_ERROR = 17
    INTERNAL_ERROR = 18


EXIT_CODE_NAMES: dict[int, str] = {int(code): code.name for code in ExitCode}
