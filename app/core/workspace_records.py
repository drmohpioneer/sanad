"""The one already-loaded record bundle behind a workspace snapshot.

The storage layer owns how this bundle is read atomically.  Projection code
only receives these records; it never calls a store and therefore cannot mix
values loaded at different moments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .models import Doctor, Event, LinkToken, Loop, Patient, Relay, Report


@dataclass(frozen=True)
class WorkspaceRecords:
    """A doctor's complete read-side input, captured at one storage boundary."""

    doctor: Doctor
    patients: tuple[Patient, ...]
    loops: tuple[Loop, ...]
    events: tuple[Event, ...]
    reports: tuple[Report, ...]
    link_tokens: tuple[LinkToken, ...]
    open_relays: tuple[Relay, ...]
    settings: Mapping[str, Any]
    # Storage-owned snapshot boundary. Firestore supplies DocumentSnapshot's
    # transaction read_time; the in-memory oracle supplies the corresponding
    # locked clock boundary. It is read-side metadata, never a persisted field.
    read_at: datetime | None = None
