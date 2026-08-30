"""Authoritative, dependency-free detection for Sanad unittest processes."""

from __future__ import annotations

import os
import sys


def _direct_test_script(argv: tuple[str, ...]) -> bool:
    for arg in argv[1:]:
        if arg in {"-c", "-m"}:
            return False
        if arg.startswith("-"):
            continue
        name = os.path.basename(arg).lower()
        parts = {part.lower() for part in os.path.normpath(arg).split(os.sep)}
        return (
            name in {"unittest", "unittest.exe"}
            or (
                name.endswith(".py")
                and "tests" in parts
                and (name.startswith("test_") or name.endswith("_test.py"))
            )
        )
    return False


def is_test_process() -> bool:
    """Return true only for explicit test mode or a supported unittest CLI."""
    explicit = os.environ.get("SANAD_TEST_MODE", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    argv = tuple(getattr(sys, "orig_argv", ()) or sys.argv)
    if any(
        argv[index] == "-m" and argv[index + 1] == "unittest"
        for index in range(len(argv) - 1)
    ):
        return True
    return _direct_test_script(argv)
