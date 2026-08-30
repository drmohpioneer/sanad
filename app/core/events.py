"""Owns the append-only event log: one writer, one reader.

Events are the history judges see. Nothing here mutates or deletes.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import Channel, Event, EventKind
from . import store


async def append_event(
    doctor_id: str,
    kind: EventKind,
    text: str = "",
    *,
    patient_id: Optional[str] = None,
    loop_id: Optional[str] = None,
    channel: Channel = "web",
    media: Optional[list[dict[str, Any]]] = None,
    meta: Optional[dict[str, Any]] = None,
    synthetic: bool = True,
) -> Event:
    event = Event(
        id=store.new_id(),
        synthetic=synthetic,
        doctor_id=doctor_id,
        patient_id=patient_id,
        loop_id=loop_id,
        kind=kind,
        channel=channel,
        text=text,
        media=media or [],
        meta=meta or {},
        ts=store.now(),
    )
    return await store.add_event(event)


def ts_ms(event: Event) -> int:
    """The timestamp the client sees. Truncated, so `since` can round-trip it."""
    return int(event.ts.timestamp() * 1000)


async def last_events(doctor_id: str, since_ms: int = 0, limit: int = 200) -> list[Event]:
    """Events for this doctor strictly newer than `since_ms`, oldest first.

    Compares the truncated millisecond value, not the raw float: Firestore keeps
    microseconds, so `ts.timestamp() * 1000 > since_ms` is still true for the very
    event that produced `since_ms` and the console redraws it on every poll.
    """
    events = await store.list_events(doctor_id)
    fresh = [e for e in events if ts_ms(e) > since_ms]
    return fresh[-limit:]
