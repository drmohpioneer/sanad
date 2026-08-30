"""Owns the end of the day: what Sanad carried, counted from the records.

No model is involved and none ever will be. Every number below is a count over
the doctor's own loops, events and relays, which is what makes it safe to show
on a screen: a summary that could be generated could be wrong, and a doctor
reading "3 patients could not be reached" has to be able to take it literally.

"Lost: zero by construction" is the claim this file has to earn, so the way it
earns it is written here and asserted in the suite. `classify` is a total
function: every loop the doctor has falls into exactly one of six buckets, the
last of which is an else. There is no path through it that returns nothing, so
the six counts always add up to the number of loops carried and nothing can
quietly fall out of the report. A test drives every state, barrier and flag
combination through it and asserts that sum.

The day is Cairo's day, not UTC's. Cairo runs two or three hours ahead, so
everything between midnight and 03:00 Cairo lands on the previous UTC date; a
critical result escalated at 01:30 used to fall out of that morning's summary
and reappear on the day before it happened (reviews/codex-troubleshoot-1.md
item 19). `today()` is the helper every caller should date a summary with.

Two counts are one count when they are one case. A barrier the Coordinator
escalated opens a bucket on the loop and a card on the relay, and both used to
be counted, so one patient who cannot afford one test was two of the doctor's
cases and one of his "treatment questions". A logistical barrier (cost,
availability, transport, forgot, in_hospital) is logistics: it counts as a
patient who needed help and not as a question. A clinical one (asymptomatic,
refuses, unclear) is the patient arguing with the treatment and stays a
question, because only the doctor can answer it. Attention cases are keyed by
patient and reason rather than by document id, so the same patient with the same
reason is one case however many records carry it.

The card wording is fixed by the spec and is not a template with judgement in
it: only the numbers move.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from . import timing

# The six buckets. The order is the precedence: a loop that is both escalated
# and unreachable is counted where the doctor's attention has to go first.
BUCKETS: tuple[str, ...] = (
    "critical",                 # a critical value went to him today
    "unreachable",              # the ladder ran out with no reply
    "needs_help",               # a barrier is recorded, reminders may be paused
    "completed_with_evidence",  # closed by the doctor, with evidence on it
    "closed_without_evidence",  # closed by the doctor, nothing attached
    "progressing",              # everything else: Sanad is still carrying it
)

# Barrier classes that are somebody's logistics rather than a clinical question.
# A patient who cannot afford the test, cannot find the drug or cannot get to
# the lab has not asked the doctor anything: he needs the doctor to solve
# something. The other barrier classes (asymptomatic, refuses, unclear) are the
# patient arguing with the treatment, and only the doctor can answer those, so
# they stay in the treatment-question count.
LOGISTICAL: tuple[str, ...] = (
    "cost", "availability", "transport", "forgot", "in_hospital",
)

# What core/coordinator._escalate writes on a relay it opens for a barrier.
BARRIER_REASON = "barrier:"

CARD_TITLE = "End of day"


def today(when: Optional[datetime] = None) -> date:
    """The doctor's day, which is Cairo's day and never UTC's.

    Cairo runs two or three hours ahead of UTC, so everything a patient sends
    between midnight and 03:00 Cairo lands on the previous UTC date. A critical
    result escalated at 01:30 fell out of that morning's summary and reappeared
    on the day before it happened (reviews/codex-troubleshoot-1.md item 19).
    """
    return (when or datetime.now(timezone.utc)).astimezone(timing.CAIRO).date()


def _on_day(event: Any, on: Optional[date]) -> bool:
    """Did this event happen on that Cairo day? An undated event always counts."""
    if on is None:
        return True
    stamp = getattr(event, "ts", None)
    if stamp is None:
        return True
    return stamp.astimezone(timing.CAIRO).date() == on


def relay_barrier(relay: Any) -> str:
    """The barrier class a relay was opened for, or "" for a plain question."""
    reason = str(getattr(relay, "reason", "") or "").strip().lower()
    if not reason.startswith(BARRIER_REASON):
        return ""
    return reason[len(BARRIER_REASON):].strip()


def is_logistical(relay: Any) -> bool:
    """Is this relay somebody's logistics rather than a treatment question?"""
    return relay_barrier(relay) in LOGISTICAL


# Which reasons are about the patient and which are about one obligation. A
# barrier is about the patient: one patient who cannot afford one test is one
# case however many records carry it, which is the double count Codex found. A
# result is about itself: two results of one patient waiting for his review are
# two things he has to read, and merging them tells him there is one (kernel
# review F14).
PER_PATIENT: tuple[str, ...] = ("needs_help", "unreachable", "question")


def _case(record: Any, reason: str) -> tuple[str, str, str]:
    """The attention key: (patient, reason, obligation), one case.

    The third part is the loop the reason belongs to, and it is deliberately
    blanked for the reasons in PER_PATIENT so those still merge across a
    patient's obligations. A record carrying no patient id is keyed on its own
    id instead, because there is nothing to deduplicate it against and folding
    all of those together would hide cases rather than merge them.
    """
    patient = str(getattr(record, "patient_id", "") or "")
    loop_id = "" if reason in PER_PATIENT else str(
        getattr(record, "loop_id", None) or getattr(record, "id", "") or ""
    )
    return (patient or f"record:{getattr(record, 'id', '')}", reason, loop_id)


def _has_evidence(loop: Any) -> bool:
    return bool(getattr(loop, "results", None) or getattr(loop, "readings", None))


def _is_critical_event(event: Any) -> bool:
    """Did this event put a critical value in front of the doctor?

    Read from the event the escalation path already writes: the concept the
    code net stamped on it, or the text it was written with. Nothing is
    re-judged here; this only counts what already happened.
    """
    if getattr(event, "kind", "") != "escalation":
        return False
    meta = getattr(event, "meta", None) or {}
    concept = str((meta.get("sentinel") or {}).get("concept") or "").lower()
    text = str(getattr(event, "text", "") or "").lower()
    return (
        "critical" in concept or "critical" in text
        or "hypertensive crisis" in concept or "low blood pressure" in concept
    )


def critical_loops(events: Iterable[Any], on: Optional[date] = None) -> set[str]:
    """The loop ids a critical value fired on, on that day."""
    out: set[str] = set()
    for event in events:
        if not _is_critical_event(event):
            continue
        if not _on_day(event, on):
            continue
        if getattr(event, "loop_id", None):
            out.add(str(event.loop_id))
    return out


def loose_criticals(events: Iterable[Any], on: Optional[date] = None) -> int:
    """Critical escalations that belong to no loop: a typed reading, mostly."""
    count = 0
    for event in events:
        if not _is_critical_event(event) or getattr(event, "loop_id", None):
            continue
        if not _on_day(event, on):
            continue
        count += 1
    return count


def duplicate_events(events: Iterable[Any], on: Optional[date] = None) -> int:
    """Identical inbound photos Sanad refused to process a second time."""
    return sum(
        1 for event in events
        if bool((getattr(event, "meta", None) or {}).get("duplicate_image"))
        and _on_day(event, on)
    )


def classify(loop: Any, criticals: set[str]) -> str:
    """One loop -> exactly one bucket. Total by construction: the last is else."""
    if str(getattr(loop, "id", "")) in criticals:
        return "critical"
    state = str(getattr(loop, "state", "open"))
    if state == "unreachable":
        return "unreachable"
    if getattr(loop, "paused", False) or (getattr(loop, "barrier", "") or ""):
        return "needs_help"
    if state == "done":
        return ("completed_with_evidence" if _has_evidence(loop)
                else "closed_without_evidence")
    return "progressing"


@dataclass
class Counts:
    """The day, in numbers. `lost` is here so it can be shown to be zero."""

    carried: int = 0
    buckets: dict[str, int] = field(default_factory=lambda: {b: 0 for b in BUCKETS})
    patients_needing_help: int = 0
    patients_unreachable: int = 0
    questions: int = 0
    criticals: int = 0
    attention: int = 0
    duplicates: int = 0

    @property
    def lost(self) -> int:
        """Carried minus every bucket. Zero because `classify` is total."""
        return self.carried - sum(self.buckets.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "carried": self.carried,
            "completed_with_evidence": self.buckets["completed_with_evidence"],
            "progressing": self.buckets["progressing"],
            "needed_help": self.patients_needing_help,
            "unreachable": self.patients_unreachable,
            "questions": self.questions,
            "criticals": self.criticals,
            "attention": self.attention,
            "closed_without_evidence": self.buckets["closed_without_evidence"],
            "buckets": dict(self.buckets),
            "lost": self.lost,
            "duplicates": self.duplicates,
        }


def compute(
    loops: Sequence[Any],
    events: Sequence[Any] = (),
    open_relays: Sequence[Any] = (),
    *,
    on: Optional[date] = None,
    duplicates: Optional[int] = None,
) -> Counts:
    """The whole day from the records. Counting, and nothing else."""
    criticals = critical_loops(events, on)
    counts = Counts(
        carried=len(loops),
        duplicates=(duplicate_events(events, on)
                    if duplicates is None else duplicates),
    )

    help_patients: set[str] = set()
    quiet_patients: set[str] = set()
    cases: set[tuple[str, str, str]] = set()

    for loop in loops:
        bucket = classify(loop, criticals)
        counts.buckets[bucket] += 1
        patient = str(getattr(loop, "patient_id", "") or "")
        if bucket == "needs_help":
            help_patients.add(patient)
            cases.add(_case(loop, "needs_help"))
        elif bucket == "unreachable":
            quiet_patients.add(patient)
            cases.add(_case(loop, "unreachable"))
        elif bucket == "critical":
            cases.add(_case(loop, "critical"))
        elif str(getattr(loop, "state", "")) == "pending_review":
            cases.add(_case(loop, "pending_review"))

    # A relay opened for a logistical barrier is the same case as the loop it
    # paused: one patient, one reason. It is counted as logistical help and not
    # as a treatment question, and it does not add a second case for a patient
    # the loop already put on the list.
    questions = 0
    for relay in open_relays:
        if is_logistical(relay):
            patient = str(getattr(relay, "patient_id", "") or "")
            if patient:
                help_patients.add(patient)
            cases.add(_case(relay, "needs_help"))
            continue
        questions += 1
        cases.add(_case(relay, "question"))

    counts.patients_needing_help = len(help_patients)
    counts.patients_unreachable = len(quiet_patients)
    counts.questions = questions
    # A critical result the doctor saw today: one per loop it fired on, plus
    # the ones that belong to no loop (a typed blood pressure with no monitor
    # loop open). Those cannot be in the loop partition, so they are counted
    # here and not there, and the partition is still exactly the loops.
    counts.criticals = counts.buckets["critical"] + loose_criticals(events, on)
    counts.attention = len(cases)
    return counts


# The wording is fixed by the spec, word for word. Only the numbers move.
LINE = (
    "Today Sanad carried {carried} care obligations · {completed} completed "
    "with evidence · {progressing} progressing normally · {help} patients "
    "needed logistical help · {quiet} patients could not be reached · "
    "{questions} treatment questions need you · {criticals} critical results "
    "escalated · Doctor attention required: {attention} cases"
)


def line(counts: Counts) -> str:
    """The one sentence, exactly as the spec writes it."""
    return LINE.format(
        carried=counts.carried,
        completed=counts.buckets["completed_with_evidence"],
        progressing=counts.buckets["progressing"],
        help=counts.patients_needing_help,
        quiet=counts.patients_unreachable,
        questions=counts.questions,
        criticals=counts.criticals,
        attention=counts.attention,
    )


def card(counts: Counts, doctor_name: str = "", when: Optional[datetime] = None) -> dict:
    """The doctor's end-of-day card. The line, then how it was counted."""
    stamp = f"{when:%Y-%m-%d}" if when else ""
    return {
        "title": f"{CARD_TITLE} · {doctor_name}".strip(" ·") + (f" · {stamp}" if stamp else ""),
        "severity": "yellow" if counts.attention else "green",
        "lines": [
            line(counts),
            f"Lost: {counts.lost}. Every obligation carried is in exactly one "
            "bucket, so this number is zero by construction, not by hope "
            "(core/summary.py).",
            f"Duplicate inbound photos ignored: {counts.duplicates}.",
            "Counted from the records in code. No model was asked.",
        ],
        "actions": [],
    }
