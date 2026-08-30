"""Owns the completion report: one message when a patient's last loop closes.

The body is built in code from the board and the stored events - every loop with
its outcome, every value the extractor read with the doctor's target beside it,
the monitoring table, and any escalation that happened. None of that is
generated, so none of it can be invented.

One claim is forbidden outright and the code, not the prompt, is what prevents
it: Sanad never observes a patient swallowing anything, so a MEDICATION loop is
reported as prescribed, never as taken, and the report says so in a fixed line
that the model cannot remove.
"""

from __future__ import annotations

from typing import Optional

from . import events, monitoring, names, settings, store, timing
from .adapters import OutboundMessage, fanout
from .models import Doctor, Loop, Patient, Report

NO_ADHERENCE_LINE = (
    "Sanad has no way to observe medication being taken, so nothing in this "
    "report is a statement about adherence."
)

OUTCOME = {
    "done": "closed, reviewed by you",
    "pending_review": "result in, waiting for your review",
    "received": "result received",
    "waiting_patient": "reminders sent, no result yet",
    "unreachable": "unreachable after three reminders",
    "open": "open",
}

def completion_title(patient: Patient) -> str:
    """The one place a completion report is named.

    The Reports screen used to find these by matching this wording against the
    first line of an event. It reads the stored record now, and this function
    exists so the title on the record and the heading in the body are the same
    string rather than two strings that happen to agree today.
    """
    return f"Completion report: {patient.name}"


def _loop_block(loop: Loop, time_scale: int = timing.REAL_DAY_SECONDS) -> list[str]:
    """One loop, its outcome, and whatever came back on it."""
    due = f", due {loop.due_at:%Y-%m-%d}" if loop.due_at else ""
    lines = [f"{loop.type}: {loop.title} · {OUTCOME.get(loop.state, loop.state)}{due}"]
    for row in loop.results or []:
        lines.append(f"    {row.get('line') or row.get('analyte')}")
    readings = loop.readings or []
    if readings:
        lines.append(f"    readings ({len(readings)}):")
        for row in readings:
            lines.append(f"      {str(row.get('at', ''))[:16]}  {row.get('value', '')}")
    # A monitoring loop gets the whole S6++ item H summary, which already
    # carries the trend and says what was asked for and what did not arrive. A
    # loop that is not a monitor has a schedule nobody wrote, so it keeps the
    # first against last line it always had.
    if monitoring.is_monitoring(loop):
        lines.append(f"    {monitoring.line(loop, time_scale)}")
    elif readings:
        lines.append(f"    {trend(readings)}")
    return lines


def trend(readings: list[dict]) -> str:
    """First against last, in code. Two readings are a direction, not a trend."""
    numbers = [r.get("number") for r in readings
               if isinstance(r.get("number"), (int, float))]
    if len(numbers) < 2:
        return "trend: not enough readings"
    change = numbers[-1] - numbers[0]
    word = "no change" if change == 0 else ("down" if change < 0 else "up")
    return f"trend: {word} ({numbers[0]} -> {numbers[-1]} across {len(numbers)} readings)"


async def build(doctor: Doctor, patient: Patient) -> str:
    """The whole report, deterministically assembled from stored records."""
    loops = await store.list_loops(patient.id)
    history = await events.last_events(doctor.id, 0)
    # The rehearsal's own day length, so a monitoring line in a report counts
    # the same days the patient's panel counts (wave A F11).
    _, time_scale = await settings.current()
    escalations = [
        e for e in history if e.kind == "escalation" and e.patient_id == patient.id
    ]

    lines = [
        completion_title(patient)
        + (f" ({patient.diagnosis})" if patient.diagnosis else ""),
        f"for {doctor.name}, {store.now():%Y-%m-%d}",
        "",
    ]
    if patient.targets:
        lines.append("Targets: " + ", ".join(f"{k} {v}" for k, v in patient.targets.items()))
    if patient.baseline:
        lines.append("Baseline: " + ", ".join(f"{k} {v}" for k, v in patient.baseline.items()))
    if patient.targets or patient.baseline:
        lines.append("")

    for loop in loops:
        lines += _loop_block(loop, time_scale)
    lines.append("")

    if escalations:
        lines.append(f"Escalations ({len(escalations)}):")
        for event in escalations:
            lines.append(f"  {event.ts:%Y-%m-%d %H:%M} {event.text}")
    else:
        lines.append("Escalations: none.")

    lines += ["", NO_ADHERENCE_LINE]
    return "\n".join(lines)


async def find_patient(doctor: Doctor, fragment: str) -> tuple[Optional[Patient], str]:
    """The one patient the doctor meant, or None and the line to send him back.

    Ambiguity is answered, never guessed at (core/names.py): "/report Ismail"
    with both an Ismail Roshdy and a Hend Ismail on the board names them both
    and asks for more of the name, instead of reporting on whichever of the two
    was registered first.
    """
    everyone = await store.list_patients(doctor.id)
    match = names.resolve([p.name for p in everyone], fragment)
    if match.ambiguous:
        return None, match.warning()
    if match.one is None:
        return None, match.nobody()
    return next(p for p in everyone if p.name == match.one), ""


async def record(
    doctor: Doctor, kind: str, title: str, body: str,
    patient_id: Optional[str] = None,
) -> Report:
    """Store one report at the moment it is written.

    Called from both places a report is produced: here when the last loop
    closes, and from core/dispatch.py when the doctor asks for a digest or for
    a named patient's report. Storing it at creation is what lets
    GET /c/{token}/reports ask the database what a report is, instead of
    matching the first line of an event against wording that can be reworded.
    """
    return await store.save_report(Report(
        id=store.new_id(), doctor_id=doctor.id, kind=kind,
        patient_id=patient_id, title=title, body=body, created_at=store.now(),
    ))


async def send_if_complete(doctor: Doctor, patient: Patient) -> bool:
    """Called whenever a loop closes. One report, only when the last one is done.

    "Complete" means no loop is still live. An unreachable loop is not live: it
    is a loop Sanad has stopped chasing, and the report says so.
    """
    loops = await store.list_loops(patient.id)
    if not loops or any(l.state in ("open", "waiting_patient", "received",
                                    "pending_review") for l in loops):
        return False
    text = await build(doctor, patient)
    stored = await record(doctor, "completion", completion_title(patient), text,
                          patient_id=patient.id)
    await fanout().send(f"doctor:{doctor.web_token}", OutboundMessage(
        text=text, meta={"report": {"patient_id": patient.id, "id": stored.id}}))
    await events.append_event(
        doctor.id, "system", f"completion report sent for {patient.name}",
        patient_id=patient.id,
    )
    return True
