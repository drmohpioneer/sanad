"""Owns when a nudge is allowed to leave: the clock rules, as pure functions.

Three rules live here, all from the locked spec and none of them a prompt:

  - the ladder: two days before due, on the due day, three days after;
  - quiet hours: nothing reaches a patient between 22:00 and 09:00 Cairo time;
  - one message per patient per "day".

`TIME_SCALE` is how many real seconds make one Sanad "day". At the default
86400 a day is a day and the rules are the real ones. A rehearsal sets it to a
few seconds so the whole ladder plays out while the judges watch; the logic
above does not change, only the length of a day.

Quiet hours are checked only at real scale. A compressed day has no wall clock
to be quiet in - eleven "days" can pass inside a minute - so at any other scale
the check is skipped and the rehearsal is honest about it.

No I/O, no cloud SDK: this module and its tests run anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")

REAL_DAY_SECONDS = 86400
# The locked ladder, in days relative to the loop's due date.
LADDER_DAYS: tuple[int, ...] = (-2, 0, 3)
QUIET_FROM_HOUR = 22  # 22:00 Cairo, inclusive
QUIET_UNTIL_HOUR = 9  # 09:00 Cairo, exclusive


def seconds(days: float, time_scale: int) -> float:
    """Sanad days -> real seconds at the current scale."""
    return days * time_scale


def in_quiet_hours(when: datetime, time_scale: int = REAL_DAY_SECONDS) -> bool:
    """True when a nudge must wait. Always False when time is compressed."""
    if time_scale != REAL_DAY_SECONDS:
        return False
    hour = when.astimezone(CAIRO).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def next_allowed(when: datetime, time_scale: int = REAL_DAY_SECONDS) -> datetime:
    """The first moment at or after `when` that is not inside quiet hours."""
    if not in_quiet_hours(when, time_scale):
        return when
    local = when.astimezone(CAIRO)
    target = local.replace(hour=QUIET_UNTIL_HOUR, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_FROM_HOUR:  # late evening: 09:00 is tomorrow morning
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def day_index(when: datetime, time_scale: int = REAL_DAY_SECONDS) -> int:
    """Which "day" this instant belongs to, for the one-message-per-day rule.

    At real scale that is the Cairo calendar day, because the patient lives in
    Cairo. At any other scale it is the scaled bucket the instant falls in.
    """
    if time_scale == REAL_DAY_SECONDS:
        return when.astimezone(CAIRO).date().toordinal()
    return int(when.timestamp() // max(time_scale, 1))


def ladder_delays(due_in_days: float, time_scale: int) -> list[tuple[int, float]]:
    """(attempt number, delay in real seconds from now) for the three nudges.

    A nudge whose moment has already passed - a loop committed one day before it
    is due, so "two days before" is yesterday - is not skipped and not sent late:
    it is clamped to now, which is what a doctor expects from a system that was
    handed the patient late.
    """
    out: list[tuple[int, float]] = []
    for attempt, offset in enumerate(LADDER_DAYS, start=1):
        out.append((attempt, max(0.0, seconds(due_in_days + offset, time_scale))))
    return out


# --------------------------------------------------------------------------- #
# A date a patient reads, rev 17 item 10
# --------------------------------------------------------------------------- #
# "2026-09-01" is not a date to a fifty-year-old in Cairo; "الأحد ١ سبتمبر" is,
# and the weekday is the part he actually uses, because the weekday is what the
# lab is shut until. Seeing Sanad say the weekday back is also the proof that it
# read "المعمل مقفول لحد الأحد" rather than pattern-matched it.
#
# Rendered in Cairo time, because the patient is in Cairo and the schedule guard
# in core/policy.py already thinks in Cairo days. Nothing here decides anything:
# the date is still exactly the date the guard allowed.
WEEKDAYS_AR: tuple[str, ...] = (
    "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد",
)
MONTHS_AR: tuple[str, ...] = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)
# Egyptians read both sets of digits. Arabic-Indic is the one used here, and it
# is used only for a date: the emergency block's "123" stays Western, because a
# number a frightened patient has to dial is not a place to be clever.
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def in_words(when: datetime, speak: str) -> str:
    """A moment -> the words a patient reads for it, in his own language.

    Arabic: "الأحد ١ سبتمبر". English: "Sunday 1 September". No year, because a
    contact inside the doctor's window is always within weeks and the year adds
    only noise; no time, because the template promises a day and not an hour.
    """
    local = when.astimezone(CAIRO)
    if speak == "ar":
        day = str(local.day).translate(ARABIC_DIGITS)
        return (f"{WEEKDAYS_AR[local.weekday()]} {day} "
                f"{MONTHS_AR[local.month - 1]}")
    return f"{local:%A} {local.day} {local:%B}"
