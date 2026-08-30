"""Typed command dispatch with injected authorization and replay storage.

The bus imports no route, provider, database, or specialist. The composition
root supplies those policies and handlers, which keeps provider callbacks from
acquiring a second domain path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Optional, Protocol

from .channel_contracts import Command, CommandResult, CommandStatus


Handler = Callable[[Command], Awaitable[CommandResult]]
Authorizer = Callable[[Command], Awaitable[Optional[CommandResult]]]


@dataclass(frozen=True)
class ReplayClaim:
    state: Literal["CLAIMED", "IN_FLIGHT", "COMPLETED"]
    result: Optional[CommandResult] = None


class ReplayLedger(Protocol):
    async def claim(self, command: Command) -> ReplayClaim: ...

    async def complete(self, command: Command, result: CommandResult) -> None: ...

    async def release(self, command: Command) -> None: ...


class CommandBus:
    def __init__(
        self,
        handlers: Optional[dict[str, Handler]] = None,
        *,
        authorizer: Authorizer,
        replay: ReplayLedger,
    ) -> None:
        self._handlers: dict[str, Handler] = dict(handlers or {})
        self._authorizer = authorizer
        self._replay = replay

    def register(self, kind: str, handler: Handler) -> None:
        name = (kind or "").strip()
        if not name:
            raise ValueError("a command handler needs a kind")
        if name in self._handlers:
            raise ValueError(f"command handler already registered: {name}")
        self._handlers[name] = handler

    async def execute(self, command: Command) -> CommandResult:
        handler = self._handlers.get(command.kind)
        if handler is None:
            return CommandResult.rejected(
                "unknown_command", f"no handler for {command.kind}"
            )

        refused = await self._authorizer(command)
        if refused is not None:
            return refused

        claim = await self._replay.claim(command)
        if claim.state == "COMPLETED":
            return claim.result or CommandResult.conflict("completed_without_result")
        if claim.state == "IN_FLIGHT":
            return claim.result or CommandResult.conflict()
        if claim.state != "CLAIMED":
            raise RuntimeError(f"unknown replay claim state: {claim.state}")

        try:
            result = await handler(command)
            if not isinstance(result, CommandResult):
                raise TypeError("command handlers must return CommandResult")
        except BaseException:
            # An exception is ambiguous: legacy work may have committed before
            # a send, receipt, or response failed. Keep the claim in flight so
            # a retry cannot repeat unknown side effects. A handler that knows
            # no effect happened returns the typed RETRYABLE result instead.
            raise

        if result.status == CommandStatus.RETRYABLE:
            await self._replay.release(command)
        else:
            # Domain work has succeeded. If this completion write fails,
            # leave the claim in flight: releasing it would let a retry
            # execute already-committed side effects a second time.
            await self._replay.complete(command, result)
        return result
