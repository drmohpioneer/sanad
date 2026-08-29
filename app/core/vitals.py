"""Owns the blood-pressure table, the way core/labs.py owns the lab table.

Until S5 a blood pressure was filed and shown and nothing in the build ever
called one dangerous. That was a deliberate gap and it is now closed with the
same mechanism the lab values use: a fixed table, in code, applied to the number
whether the patient typed it or photographed the machine.

The cutoffs, set by Mohamed on 2026-08-29:

    systolic  >= 180    hypertensive crisis   red card AND the emergency line
    diastolic >= 120    hypertensive crisis   red card AND the emergency line
    systolic  <   90    low blood pressure    red card AND the emergency line
    anything else       filed to the chart, no card of its own

Every red row reaches the patient as well as the doctor. S5 pass 1 shipped the
low row as a card to the doctor only, reading the spec sentence literally, and
asked whether that was the intent. It was not: a systolic under 90 at home is a
reading somebody should be seen for, so both red rows send the same emergency
block the Sentinel sends and the difference between them is only the wording on
the doctor's card.

Two numbers or nothing: half a blood pressure is not a blood pressure, and a
message that is not a reading at all is not this module's business. `parse()`
matches the whole message and nothing less, which is why "my pressure was 190"
is a sentence for the Concierge and "190/125" is a measurement for this table.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

Level = Literal["crisis", "low", "normal"]

# The table. Three numbers, and they never move at runtime.
SYSTOLIC_CRISIS = 180
DIASTOLIC_CRISIS = 120
SYSTOLIC_LOW = 90

# What the card and the event say made the decision, printed verbatim so a
# judge reading the screen can find the file that decided it.
DECIDED_BY = "code (core/vitals.py blood-pressure table)"

CRISIS_CONCEPT = "hypertensive crisis"
LOW_CONCEPT = "low blood pressure"

# A typed reading is only a reading when the whole message is one. Shared with
# core/concierge.record_reading so that what gets graded and what gets filed can
# never drift apart.
BP_TEXT = re.compile(r"^\s*(\d{2,3})\s*[/\\]\s*(\d{2,3})\s*$")


@dataclass(frozen=True)
class Verdict:
    """One blood pressure, judged. `line` is what the doctor's card prints."""

    level: Level
    systolic: int
    diastolic: int
    concept: str
    line: str

    @property
    def red(self) -> bool:
        """Does the doctor get a red card for this reading?"""
        return self.level != "normal"

    @property
    def emergency(self) -> bool:
        """Does the patient get the emergency block for this reading?

        Yes for both red rows. S5 pass 1 shipped the crisis row alone and asked
        the question; the answer, on 2026-08-29, was that a systolic under 90 is
        also a reading a patient should not sit at home with. Both red rows now
        send the same emergency block the Sentinel sends, and "normal" sends
        none.
        """
        return self.level != "normal"

    def as_meta(self) -> dict:
        """The audit trail, in the shape the Sentinel's verdicts already use."""
        return {
            "fired": self.red,
            "net": "code",
            "concept": self.concept,
            "nets_run": ["code"],
            "systolic": self.systolic,
            "diastolic": self.diastolic,
            "decided_by": DECIDED_BY,
        }


def parse(text: str) -> Optional[tuple[int, int]]:
    """"185/125" -> (185, 125). Anything else, including prose, is None."""
    match = BP_TEXT.match(str(text or ""))
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def judge(systolic: int, diastolic: int) -> Verdict:
    """Two numbers -> the verdict. The only place a blood pressure is graded."""
    head = f"BP {systolic}/{diastolic} mmHg"
    if systolic >= SYSTOLIC_CRISIS or diastolic >= DIASTOLIC_CRISIS:
        return Verdict(
            level="crisis", systolic=systolic, diastolic=diastolic,
            concept=CRISIS_CONCEPT,
            line=f"{head} · CRITICAL (crisis at or above {SYSTOLIC_CRISIS} "
                 f"systolic or {DIASTOLIC_CRISIS} diastolic)",
        )
    if systolic < SYSTOLIC_LOW:
        return Verdict(
            level="low", systolic=systolic, diastolic=diastolic,
            concept=LOW_CONCEPT,
            line=f"{head} · CRITICAL (systolic below {SYSTOLIC_LOW})",
        )
    return Verdict(level="normal", systolic=systolic, diastolic=diastolic,
                   concept="", line=head)


def judge_text(text: str) -> Optional[Verdict]:
    """A whole message -> the verdict, or None when it is not a reading.

    Both paths into the table come through here: what the patient typed, and
    the "142/91" that core/photos.reading_row builds off a monitor screen. One
    entry point, so a photographed crisis and a typed one cannot be graded
    differently.
    """
    pair = parse(text)
    return None if pair is None else judge(*pair)


def red_card(patient_name: str, verdict: Verdict,
             extra_lines: Sequence[str] = ()) -> dict:
    """The doctor's card for a reading this table calls critical.

    Built here rather than in a caller because both callers need the same card:
    the Concierge for a typed reading, the Lab-Extractor for a photographed one.
    A dict in, a dict out, so it is tested with no model and no database.
    """
    return {
        "title": f"🚨 CRITICAL BP · {patient_name}",
        "severity": "red",
        "lines": [verdict.line, f"decided_by: {DECIDED_BY}", *extra_lines],
        "actions": [],
    }
