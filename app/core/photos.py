"""Owns what happens to a photo once the model has said what it is.

The model classifies the picture - lab slip, blood-pressure monitor screen,
prescription, or something else - and transcribes what is printed on it. That is
the last thing the model decides. Every routing choice below is a pure function
of two facts: the class it came back with, and which loops the patient has open.

    lab slip     + an open TEST loop     -> attach it and move that loop to review
    lab slip     + no open TEST loop     -> an "unexpected result" card, values and
                                            all, with the two buttons the doctor
                                            needs (attach to the record, or open a
                                            loop for it)
    BP monitor   + an open MONITOR loop  -> the reading joins that loop's chart
    BP monitor   + no MONITOR loop       -> the reading is shown to the doctor,
                                            unfiled
    anything else                        -> stored and relayed, unread

Which open TEST loop a slip attaches to is decided the same way, from the
analytes the slip carries rather than from the order the loops were opened: see
`open_test_loop` below.

A lab slip is always read and always compared, whether or not a test is open,
which is the rule Mohamed's first real phone test asked for: a result that
arrives without a matching order is still a result. Critical values escalate on
every one of these paths, because that decision is made by core/labs.py from the
numbers, not by the route.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional, Sequence

from . import labs
from .models import Loop, PhotoKind

Route = Literal[
    "attach_to_loop", "unexpected_result", "monitor_reading",
    "unfiled_reading", "relay",
]

# The loop states a slip is still welcome on. "unreachable" is in the list on
# purpose (S3 review, carry-over 1): a patient who stopped answering and then
# sends the photo a week later has still sent the result, and reading it is the
# whole point of having chased him.
OPEN_TEST_STATES: tuple[str, ...] = (
    "open", "waiting_patient", "received", "unreachable",
)
# A monitoring loop accepts readings for as long as it is not closed.
OPEN_MONITOR_STATES: tuple[str, ...] = (
    "open", "waiting_patient", "received", "unreachable",
)


def test_name_of(loop: Loop) -> str:
    """What the doctor called this test, in his own words."""
    return str((loop.details or {}).get("test_name") or loop.title)


def open_test_loop(
    loops: Sequence[Loop], analytes: Sequence[str] = ()
) -> Optional[Loop]:
    """The TEST loop this slip answers, or None when none is open.

    With one open test there is nothing to decide. With two, the slip's own
    analytes decide it: the loop whose test name shares the most words with
    them wins (core/labs.panel_overlap), so a potassium result goes to the
    electrolytes loop and a lipid panel to the lipid loop. The oldest loop wins
    a tie, which includes the tie where the slip overlaps nothing at all, so
    behaviour with a single loop or an unrecognised panel is what it was.

    S4 review, carry-over 1: before this, the oldest open loop took every slip
    regardless of what was on it.
    """
    waiting = [l for l in loops if l.type == "TEST" and l.state in OPEN_TEST_STATES]
    if not waiting:
        return None
    if len(waiting) == 1 or not analytes:
        return waiting[0]
    # max() keeps the first of equal scores and `waiting` is oldest-first, so
    # the tie-break is the old rule with no extra branch to get wrong.
    return max(waiting, key=lambda l: labs.panel_overlap(test_name_of(l), analytes))


def open_monitor_loop(loops: Sequence[Loop]) -> Optional[Loop]:
    """The oldest MONITOR loop still collecting readings, or None."""
    live = [l for l in loops if l.type == "MONITOR" and l.state in OPEN_MONITOR_STATES]
    return live[0] if live else None


def route(kind: PhotoKind, *, test_loop: bool, monitor_loop: bool) -> Route:
    """Classification + open loops -> what Sanad does with this photo.

    The whole routing table, as one function, so it can be tested without a
    model, a database or a network.
    """
    if kind == "lab_slip":
        return "attach_to_loop" if test_loop else "unexpected_result"
    if kind == "bp_monitor":
        return "monitor_reading" if monitor_loop else "unfiled_reading"
    return "relay"


# --------------------------------------------------------------------------- #
# A blood-pressure monitor's screen
# --------------------------------------------------------------------------- #
_DIGITS = re.compile(r"\d{2,3}")


def _number(text: str) -> Optional[int]:
    match = _DIGITS.search(str(text or ""))
    return int(match.group()) if match else None


def reading_row(
    systolic: str, diastolic: str, pulse: str, at: datetime
) -> Optional[dict[str, Any]]:
    """A monitor screen -> one row of the patient's chart, or None.

    Both pressures must be readable: half a blood pressure is not a reading, and
    a row with a missing number would poison the trend line in the report. The
    shape matches what a typed reading produces (core/concierge.record_reading),
    so the chart has one row format whatever the patient sent.
    """
    top, bottom = _number(systolic), _number(diastolic)
    if top is None or bottom is None:
        return None
    row: dict[str, Any] = {
        "at": at.isoformat(timespec="minutes"),
        "value": f"{top}/{bottom}",
        "number": float(top),
        "source": "monitor photo",
    }
    beat = _number(pulse)
    if beat is not None:
        row["pulse"] = beat
    return row


def reading_line(row: dict[str, Any]) -> str:
    """The sentence the doctor's card prints for one monitor reading."""
    pulse = f", pulse {row['pulse']}" if row.get("pulse") else ""
    return f"BP {row.get('value', '')} mmHg{pulse}"
