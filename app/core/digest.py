"""Owns the /digest command: one message, every patient, where each loop stands.

Plain code over the board. No model is involved, so the digest can never invent
a patient, a loop or a date.
"""

from __future__ import annotations

from . import events, store, summary
from .models import Doctor

STATE_MARK = {
    "open": "open", "waiting_patient": "waiting on patient", "received": "received",
    "pending_review": "needs your review", "done": "done", "unreachable": "unreachable",
}


def title(doctor: Doctor) -> str:
    """The one place a digest is named, for the record and for the heading."""
    return f"Digest for {doctor.name}"


async def build(doctor: Doctor) -> str:
    patients = await store.list_patients(doctor.id)
    if not patients:
        return "No patients yet."

    history = await events.last_events(doctor.id, 0)
    lines = [f"{title(doctor)}, {len(patients)} patient(s)"]

    # The end-of-day line, from the same counting the /summary screen uses, so
    # the phone and the screen can never disagree (core/summary.py).
    every_loop = []
    for patient in patients:
        every_loop += await store.list_loops(patient.id)
    counts = summary.compute(
        every_loop, history, await store.open_relays(doctor.id),
        on=summary.today(store.now()),
    )
    lines.append(summary.line(counts))

    for patient in patients:
        lines.append("")
        lines.append(f"{patient.name} ({patient.diagnosis or 'no diagnosis'})")
        loops = await store.list_loops(patient.id)
        if not loops:
            lines.append("  no open loops")
        for loop in loops:
            due = f", due {loop.due_at:%Y-%m-%d}" if loop.due_at else ""
            lines.append(
                f"  {loop.type}: {loop.title} · {STATE_MARK.get(loop.state, loop.state)}{due}"
            )
        last = [e for e in history if e.patient_id == patient.id]
        if last:
            event = last[-1]
            lines.append(f"  last: [{event.kind}] {event.text[:120]} ({event.ts:%Y-%m-%d %H:%M})")
    return "\n".join(lines)
