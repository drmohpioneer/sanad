"""Unit tests for the two gates that must never depend on a model.

`Borrowable` below is the one shared piece, and it exists because of how the
acceptance suite is written. tests/test_codex_races.py is an independent
auditor's file: it does not subclass the harnesses in this package, it borrows
them by hand, constructing the case, calling `setUp()`, driving it, and calling
`doCleanups()` in a `finally`.

A case driven that way has no asyncio runner, because a runner is created by
`_callSetUp` and never by `setUp`. `IsolatedAsyncioTestCase._callMaybeAsync`
asserts on one, and `doCleanups` swallows that assertion into the outcome, so
every patch a borrowed harness started stays started and leaks into whatever
test runs next. Alphabetically that is most of this package, which is why one
file of eleven tests could turn thirty-eight unrelated tests red without
touching a line of them.

So a harness that is meant to be borrowed inherits this, and lends the
cleanups a runner of their own for the length of the undo.
"""

from __future__ import annotations

import asyncio
import unittest

from sanad_test_process import is_test_process


# unittest discovery imports this package before any ``tests.test_*`` module.
# The fallback remains inert when production code merely imports ``tests``.
if is_test_process():
    from sanad_test_guard import install as install_hermetic_guards

    install_hermetic_guards("tests package fallback")


class Borrowable(unittest.IsolatedAsyncioTestCase):
    """An async test case whose cleanups run even when it is driven by hand."""

    def doCleanups(self) -> bool:
        if getattr(self, "_asyncioRunner", None) is not None:
            return super().doCleanups()
        runner = asyncio.Runner()
        self._asyncioRunner = runner
        try:
            return super().doCleanups()
        finally:
            self._asyncioRunner = None
            runner.close()
