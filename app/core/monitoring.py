"""Owns the monitoring summary: a week of readings, counted in code.

S6++ item H, in the shape the spec writes it and no other:

    Requested: BP twice daily for seven days · Expected readings: 14 ·
    Received: 11 · Missing: evenings on days 3, 5 and 6 · Trend: 152/94 to
    139/86 · Threshold alerts: 1 · Patient-reported barrier: none

Every number in it is a count over the loop's own record. Nothing is generated,
because a doctor reading "Missing: evenings on days 3, 5 and 6" has to be able
to take it literally, and a sentence that could be generated could be wrong.

Two decisions worth stating, because they are judgements and not facts:

  which day a reading belongs to  the days are counted from the day the loop
                                  was created, in Cairo, because that is the
                                  day the doctor asked;
  which slot of the day it is     "twice a day" is a morning and an evening,
                                  split at noon Cairo. Three a day is morning,
                                  afternoon and evening. A schedule this file
                                  cannot read is once a day, which is the
                                  reading it can never call missing wrongly.

Threshold alerts are counted by core/vitals.py, the same blood-pressure table
that decides a red card, so the number here and the cards the doctor already
saw come from one place.

Pure functions, no I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from . import timing, vitals

NONE = "none"

# How the doctor's own words become a number of readings a day.
PER_DAY_WORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (4, ("four times", "4 times", "qid", "اربع مرات", "أربع مرات")),
    (3, ("three times", "3 times", "tds", "tid", "ثلاث مرات", "تلات مرات")),
    (2, ("twice", "two times", "2 times", "bd", "bid", "مرتين")),
    (1, ("once", "one time", "1 time", "daily", "every day", "od",
         "مره", "مرة", "يوميا", "كل يوم")),
)

SLOT_NAMES: dict[int, tuple[str, ...]] = {
    1: ("reading",),
    2: ("morning", "evening"),
    3: ("morning", "afternoon", "evening"),
    4: ("morning", "midday", "afternoon", "evening"),
}

# The hour, Cairo, at which each slot of the day ends. The last slot runs to
# midnight and needs no bound.
SLOT_BOUNDS: dict[int, tuple[int, ...]] = {
    2: (12,),
    3: (11, 16),
    4: (11, 14, 18),
}

NUMBER_WORDS: tuple[str, ...] = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def days_word(days: int) -> str:
    """Seven, not 7: the requested line reads as the doctor said it."""
    if 0 <= days < len(NUMBER_WORDS):
        return NUMBER_WORDS[days]
    if 20 < days < 30:
        return f"twenty {NUMBER_WORDS[days - 20]}"
    if days == 30:
        return "thirty"
    return str(days)


def per_day(schedule: str) -> int:
    """"twice a day" -> 2. Anything this cannot read is once a day."""
    text = " " + re.sub(r"[^0-9a-z؀-ۿ]+", " ",
                        str(schedule or "").lower()).strip() + " "
    for number, words in PER_DAY_WORDS:
        for word in words:
            if f" {word} " in text or word in text:
                return number
    return 1


def schedule_words(number: int) -> str:
    return {1: "once daily", 2: "twice daily", 3: "three times daily",
            4: "four times daily"}.get(number, f"{number} times daily")


def slot_names(number: int) -> tuple[str, ...]:
    return SLOT_NAMES.get(number) or tuple(
        f"slot {i + 1}" for i in range(number)
    )


def slot_of(hour: int, number: int) -> int:
    """Which slot of the day an hour belongs to."""
    bounds = SLOT_BOUNDS.get(number)
    if bounds is None:
        if number <= 1:
            return 0
        return min(number - 1, (hour * number) // 24)
    for index, bound in enumerate(bounds):
        if hour < bound:
            return index
    return len(bounds)


def _when(row: Any) -> Optional[datetime]:
    raw = (row or {}).get("at")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _plural(name: str) -> str:
    return name if name.endswith("s") or name.startswith("slot") else name + "s"


def _days_phrase(days: Sequence[int]) -> str:
    """[3, 5, 6] -> "days 3, 5 and 6". One day is "day 3"."""
    numbers = [str(d) for d in days]
    if len(numbers) == 1:
        return f"day {numbers[0]}"
    return f"days {', '.join(numbers[:-1])} and {numbers[-1]}"


def _average(numbers: Sequence[float]) -> float:
    return sum(numbers) / len(numbers)


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _whole(value: float) -> str:
    """A blood pressure is read as whole numbers, so an average is rounded.

    Half up, not Python's half to even: "144/89" is what a doctor writes, and a
    reader should not have to know which way 143.5 went.
    """
    return str(int(float(value) + 0.5))


# "143/89", "143 / 89", and the same with a pulse after it. The second number is
# what a BP row carries that a weight row does not.
_PAIR = re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})")


def _diastolic(row: Any) -> Optional[float]:
    """The lower number printed on this reading, when there is one."""
    found = _PAIR.search(str((row or {}).get("value") or ""))
    return float(found.group(2)) if found else None


def trend_of(rows: Sequence[Any]) -> str:
    """First three readings against the last three (rev 18 item c).

    A blood pressure is a pair and it is read as a pair: "144/89 to 138/83",
    the average systolic and the average diastolic of the first three and of the
    last three. The board printed the systolic averages alone ("143.7 to
    138.3"), which is a real number about half a reading, and a doctor comparing
    it with the pair on the card had to hold the other half in his head.

    A metric that is not a pair keeps the single number it always had: a daily
    weight is "86 to 84" and inventing a second number for it would be worse
    than useless. Mixed rows, where some readings printed a pair and some did
    not, also fall back to the single number, because an average over three
    readings of which one has no diastolic is not the average of anything.
    """
    readings = [(float(row["number"]), _diastolic(row)) for row in rows
                if isinstance((row or {}).get("number"), (int, float))]
    if len(readings) < 2:
        return "not enough readings"
    first, last = readings[:3], readings[-3:]
    if all(diastolic is not None for _, diastolic in readings):
        return (f"{_whole(_average([s for s, _ in first]))}/"
                f"{_whole(_average([d for _, d in first]))} to "
                f"{_whole(_average([s for s, _ in last]))}/"
                f"{_whole(_average([d for _, d in last]))}")
    return (f"{_number(_average([s for s, _ in first]))} to "
            f"{_number(_average([s for s, _ in last]))}")


@dataclass(frozen=True)
class Monitoring:
    """One monitoring loop, counted. `line` is the sentence the spec fixes."""

    metric: str
    schedule: str
    days: int
    per_day: int
    expected: int
    received: int
    missing: str
    missing_slots: tuple[tuple[int, int], ...]
    trend: str
    alerts: int
    barrier: str

    @property
    def line(self) -> str:
        return (
            f"Requested: {self.metric} {self.schedule} for "
            f"{days_word(self.days)} day{'' if self.days == 1 else 's'} · "
            f"Expected readings: {self.expected} · Received: {self.received} · "
            f"Missing: {self.missing} · Trend: {self.trend} · "
            f"Threshold alerts: {self.alerts} · "
            f"Patient-reported barrier: {self.barrier}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric, "schedule": self.schedule, "days": self.days,
            "per_day": self.per_day, "expected": self.expected,
            "received": self.received, "missing": self.missing,
            "missing_slots": [list(slot) for slot in self.missing_slots],
            "trend": self.trend, "alerts": self.alerts,
            "barrier": self.barrier, "line": self.line,
        }


# How many days after a MONITOR loop is created its first reminder goes out.
# One, and it is not a choice made here: core/chaser.schedule_loop enqueues the
# reminders for day in range(1, days + 1). The two numbers have to be the same
# number or the doctor's summary calls a day missing that nobody was asked on.
FIRST_REMINDER_DAY = 1


def is_monitoring(loop: Any) -> bool:
    return str(getattr(loop, "type", "")) == "MONITOR"


def slot_at(when: datetime, number: int, time_scale: int) -> int:
    """Which slot of the day an instant belongs to, at whatever a day is.

    At real time a slot is a stretch of the clock and `slot_of` names it by the
    Cairo hour. At any compressed scale there is no clock to divide: eleven
    "days" can pass inside a minute, so the slot is the position inside the
    scaled bucket, which is the same division by another name.
    """
    if time_scale == timing.REAL_DAY_SECONDS:
        return slot_of(when.astimezone(timing.CAIRO).hour, number)
    if number <= 1:
        return 0
    period = max(time_scale, 1)
    fraction = (when.timestamp() % period) / period
    return min(number - 1, int(fraction * number))


def summary(loop: Any, time_scale: int = timing.REAL_DAY_SECONDS) -> Monitoring:
    """One MONITOR loop -> its whole summary. Counting, and nothing else.

    `time_scale` is how many real seconds make one Sanad day (core/settings.py).
    It has to be the same number the chaser scheduled the reminders with, or the
    summary counts days nobody was asked on: at the runbook's rehearsal scale of
    three seconds a day, every reading of the whole run falls on one Cairo
    calendar date, and counting by calendar found none of them (kernel review
    F11). The default is real time, so nothing that does not pass it changes.
    """
    details = getattr(loop, "details", None) or {}
    metric = str(details.get("metric") or getattr(loop, "title", "") or "readings")
    number = per_day(str(details.get("schedule") or ""))
    try:
        days = int(details.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    days = max(days, 1)
    expected = number * days

    # Day 1 is the first day Sanad ASKED for a reading, not the day the doctor
    # dictated the plan. core/chaser.schedule_loop queues a MONITOR loop's
    # reminders at timing.seconds(day, scale) for day in 1..N, so the first one
    # reaches the patient one Sanad day after the loop is created and the last
    # one N days after. Counting from the creation date put the whole window a
    # day early, and the last day the patient was actually asked on fell outside
    # it (reviews/codex-troubleshoot-1.md item 8).
    #
    # The bucket is timing.day_index, which is the Cairo calendar day at real
    # time and the scaled bucket at any other scale, so the two ends of the same
    # reminder agree at both. That is deliberately not "the nearest reminder
    # instant": at real time a twice-a-day schedule puts the evening reading
    # twelve hours from its own day's reminder and twelve from the next one, and
    # nearest-instant would file half the week on the wrong day.
    #
    # Two known residuals, both real-time only, both left as they are and
    # written down rather than half fixed (kernel review F12). Egypt moves the
    # clock twice a year; a loop created inside the one-hour window before
    # midnight on one of those two nights has its 24-hour reminders land on a
    # calendar day one off, so one day reads missing. And a patient who answers
    # the evening reminder after midnight is counted on the next day, so the
    # last day shows an evening missing. Both predate this wave. Fixing them
    # means giving a reading a tolerance around its own reminder instant, which
    # is a change to what "a day" means on the doctor's card, and that is
    # Mohamed's call and not an agent's.
    started = getattr(loop, "created_at", None)
    start_bucket = (
        timing.day_index(started, time_scale) + FIRST_REMINDER_DAY
        if isinstance(started, datetime) else None
    )

    rows = list(getattr(loop, "readings", None) or [])
    received = len(rows)
    filled: set[tuple[int, int]] = set()
    for row in rows:
        when = _when(row)
        if when is None or start_bucket is None:
            continue
        day = timing.day_index(when, time_scale) - start_bucket + 1
        if 1 <= day <= days:
            filled.add((day, slot_at(when, number, time_scale)))

    missing_slots = tuple(sorted(
        (day, slot)
        for day in range(1, days + 1)
        for slot in range(number)
        if (day, slot) not in filled
    ))

    trend = trend_of(rows)

    alerts = 0
    for row in rows:
        verdict = vitals.judge_text(str(row.get("value") or ""))
        if verdict is not None and verdict.red:
            alerts += 1

    return Monitoring(
        metric=metric,
        schedule=schedule_words(number),
        days=days,
        per_day=number,
        expected=expected,
        received=received,
        missing=missing_phrase(missing_slots, number, expected, received),
        missing_slots=missing_slots,
        trend=trend,
        alerts=alerts,
        barrier=str(getattr(loop, "barrier", "") or "") or NONE,
    )


def missing_phrase(missing_slots: Sequence[tuple[int, int]], number: int,
                   expected: int, received: int) -> str:
    """"evenings on days 3, 5 and 6". Nothing missing is "none"."""
    if not missing_slots:
        return NONE
    if received == 0:
        return f"all {expected} readings"

    names = slot_names(number)
    by_day: dict[int, set[int]] = {}
    for day, slot in missing_slots:
        by_day.setdefault(day, set()).add(slot)

    whole_days = sorted(day for day, slots in by_day.items()
                        if len(slots) == number)
    parts: list[str] = []
    if whole_days:
        parts.append(f"every reading on {_days_phrase(whole_days)}")

    by_slot: dict[int, list[int]] = {}
    for day, slot in missing_slots:
        if day in whole_days:
            continue
        by_slot.setdefault(slot, []).append(day)
    for slot in sorted(by_slot):
        name = names[slot] if slot < len(names) else f"slot {slot + 1}"
        days = sorted(by_slot[slot])
        word = _plural(name) if len(days) > 1 else name
        parts.append(f"{word} on {_days_phrase(days)}")
    return "; ".join(parts)


def line(loop: Any, time_scale: int = timing.REAL_DAY_SECONDS) -> str:
    """The one sentence, for a MONITOR loop. Empty for anything else.

    `time_scale` is the same argument `summary` takes and for the same reason
    (wave A F11): a caller that cannot reach core/settings.py gets real time,
    which is the default and no worse off than before, and a caller that can
    hands the rehearsal's own scale through so the sentence names the days the
    patient was actually asked on.
    """
    return summary(loop, time_scale).line if is_monitoring(loop) else ""
