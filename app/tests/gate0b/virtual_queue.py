"""A deterministic, inspectable replacement for Gate 0B's Cloud Tasks seam."""

from __future__ import annotations

import copy
import math
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional
from unittest.mock import patch

from core import tasks


VIRTUAL_TASK_AUTHORIZATION = "Bearer gate0b-hermetic-task"


class VirtualQueueViolation(BaseException):
    """A queue use outside the frozen local scenario (not catchable fallback)."""


@dataclass
class VirtualTask:
    sequence: int
    name: str
    path: str
    payload: dict[str, Any]
    delay_seconds: float
    created_at: datetime
    scheduled_at: datetime
    state: str = "pending"
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    status_code: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        for key in ("created_at", "scheduled_at", "dispatched_at", "completed_at"):
            value = row[key]
            row[key] = value.isoformat() if value is not None else None
        return row


@dataclass(frozen=True)
class VerificationCall:
    sequence: int
    authorization: str
    verified_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "authorization": self.authorization,
            "verified_at": self.verified_at.isoformat(),
        }


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class VirtualTaskQueue:
    """Records every task and lets the scenario dispatch it in due order.

    ``clock`` is the same fixed/advanceable clock patched into ``core.store`` by
    the scenario.  The first twelve task identities stay distinct from later
    tasks, so Beat 3's one resumed-ladder task may remain pending while the
    initial ladder is popped and audited.
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self.tasks: list[VirtualTask] = []
        self.verifications: list[VerificationCall] = []

    def _now(self) -> datetime:
        return _utc(self._clock())

    async def enqueue(
        self, path: str, payload: dict[str, Any], delay_seconds: float
    ) -> str:
        if path != "/tasks/nudge":
            raise VirtualQueueViolation(f"unexpected virtual task path: {path!r}")
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError):
            raise VirtualQueueViolation(f"invalid task delay: {delay_seconds!r}")
        if not math.isfinite(delay):
            raise VirtualQueueViolation(f"non-finite task delay: {delay!r}")
        delay = max(0.0, delay)
        now = self._now()
        body = tasks.body_for(copy.deepcopy(payload or {}), delay, now=now)
        sequence = len(self.tasks) + 1
        name = f"gate0b/tasks/task-{sequence:04d}"
        self.tasks.append(VirtualTask(
            sequence=sequence,
            name=name,
            path=path,
            payload=body,
            delay_seconds=delay,
            created_at=now,
            scheduled_at=now + timedelta(seconds=min(delay, tasks.MAX_DELAY_SECONDS)),
        ))
        return name

    async def verify_caller(self, authorization: Optional[str]) -> dict[str, Any]:
        supplied = (authorization or "").strip()
        if supplied != VIRTUAL_TASK_AUTHORIZATION:
            raise VirtualQueueViolation(
                "virtual task handler requires VIRTUAL_TASK_AUTHORIZATION"
            )
        self.verifications.append(VerificationCall(
            sequence=len(self.verifications) + 1,
            authorization=supplied,
            verified_at=self._now(),
        ))
        return {
            "email": "gate0b-task@local.invalid",
            "email_verified": True,
            "aud": "http://sanad.test",
            "sub": "gate0b-hermetic-task",
        }

    @contextmanager
    def patch(self) -> Iterator["VirtualTaskQueue"]:
        with ExitStack() as stack:
            stack.enter_context(patch.object(tasks, "enqueue", self.enqueue))
            stack.enter_context(patch.object(tasks, "verify_caller", self.verify_caller))
            yield self

    def _pending_sorted(self) -> list[VirtualTask]:
        return sorted(
            (task for task in self.tasks if task.state == "pending"),
            key=lambda task: (task.scheduled_at, task.sequence),
        )

    def pending(self) -> list[VirtualTask]:
        return list(self._pending_sorted())

    def dispatch(
        self,
        task_or_name: VirtualTask | str,
        *,
        at: Optional[datetime] = None,
    ) -> VirtualTask:
        """Transition one pending task after the scenario advances its clock."""
        name = task_or_name.name if isinstance(task_or_name, VirtualTask) else task_or_name
        task = next((item for item in self.tasks if item.name == name), None)
        if task is None:
            raise VirtualQueueViolation(f"unknown virtual task: {name!r}")
        if task.state != "pending":
            raise VirtualQueueViolation(
                f"virtual task {name} cannot dispatch from state {task.state}"
            )
        moment = _utc(at) if at is not None else self._now()
        if moment < task.scheduled_at:
            raise VirtualQueueViolation(
                f"virtual task {name} dispatched before {task.scheduled_at.isoformat()}"
            )
        task.state = "dispatched"
        task.dispatched_at = moment
        return task

    def pop_next(self) -> VirtualTask:
        pending = self._pending_sorted()
        if not pending:
            raise VirtualQueueViolation("the virtual queue is empty")
        return self.dispatch(pending[0])

    def initial_ladder(self, count: int = 12) -> list[VirtualTask]:
        """Return initial task identities in due order without mutating them."""
        initial = [task for task in self.tasks if task.sequence <= count]
        if len(initial) != count:
            raise VirtualQueueViolation(
                f"expected {count} initial ladder tasks, found {len(initial)}"
            )
        return sorted(initial, key=lambda task: (task.scheduled_at, task.sequence))

    def pop_initial_ladder(self, count: int = 12) -> list[VirtualTask]:
        """Pop only task identities 1..``count``, ordered by due time.

        Tasks enqueued later (notably the Beat 3 resumption) are intentionally
        ignored and remain pending.
        """
        initial = self.initial_ladder(count)
        not_pending = [task.name for task in initial if task.state != "pending"]
        if not_pending:
            raise VirtualQueueViolation(f"initial tasks already popped: {not_pending}")
        for task in initial:
            self.dispatch(task, at=task.scheduled_at)
        return initial

    def complete(self, task_or_name: VirtualTask | str, status_code: int = 200) -> None:
        name = task_or_name.name if isinstance(task_or_name, VirtualTask) else task_or_name
        task = next((item for item in self.tasks if item.name == name), None)
        if task is None:
            raise VirtualQueueViolation(f"unknown virtual task: {name!r}")
        if task.state != "dispatched":
            raise VirtualQueueViolation(
                f"virtual task {name} cannot complete from state {task.state}"
            )
        task.state = "completed" if 200 <= int(status_code) < 300 else "failed"
        task.status_code = int(status_code)
        task.completed_at = self._now()

    def count_summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for task in self.tasks:
            states[task.state] = states.get(task.state, 0) + 1
        return {
            "enqueued": len(self.tasks),
            "verified_dispatches": len(self.verifications),
            "states": dict(sorted(states.items())),
            "initial_ladder": min(12, len(self.tasks)),
            "post_initial": max(0, len(self.tasks) - 12),
        }

    def ledger_as_dicts(self) -> dict[str, Any]:
        return {
            "tasks": [task.as_dict() for task in self.tasks],
            "verifications": [call.as_dict() for call in self.verifications],
            "summary": self.count_summary(),
        }
