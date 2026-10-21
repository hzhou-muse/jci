"""Errors that map to jci's exit codes."""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_NOT_FOUND = 4


class JciError(Exception):
    exit_code = EXIT_USAGE


class UsageError(JciError):
    exit_code = EXIT_USAGE


class UnreachableError(JciError):
    exit_code = EXIT_UNREACHABLE


class NotFoundError(JciError):
    exit_code = EXIT_NOT_FOUND
