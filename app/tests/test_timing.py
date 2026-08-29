"""The Chaser's clock: the ladder, quiet hours, and what a "day" means.

The rehearsal knob (TIME_SCALE) changes only the length of a day. These tests
are what proves that: the same ladder, the same one-a-day rule, at both scales.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from core import timing

CAIRO = timing.CAIRO
DAY = timing.REAL_DAY_SECONDS


def cairo(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CAIRO)


class TestLadder(unittest.TestCase):
    def test_two_before_on_the_day_three_after(self) -> None:
        self.assertEqual(timing.LADDER_DAYS, (-2, 0, 3))
        delays = timing.ladder_delays(14, DAY)
        self.assertEqual([a for a, _ in delays], [1, 2, 3])
        self.assertEqual([d / DAY for _, d in delays], [12.0, 14.0, 17.0])

    def test_a_loop_committed_late_never_schedules_the_past(self) -> None:
        """Due tomorrow: "two days before" is yesterday, so it goes out now."""
        delays = dict(timing.ladder_delays(1, DAY))
        self.assertEqual(delays[1], 0.0)
        self.assertEqual(delays[2] / DAY, 1.0)
        self.assertEqual(delays[3] / DAY, 4.0)

    def test_compressed_time_changes_only_the_length_of_a_day(self) -> None:
        real = timing.ladder_delays(14, DAY)
        fast = timing.ladder_delays(14, 1)
        self.assertEqual([a for a, _ in real], [a for a, _ in fast])
        self.assertEqual([d / DAY for _, d in real], [d for _, d in fast])


class TestQuietHours(unittest.TestCase):
    def test_nothing_leaves_between_22_and_09_cairo(self) -> None:
        self.assertTrue(timing.in_quiet_hours(cairo(2026, 8, 28, 22, 1)))
        self.assertTrue(timing.in_quiet_hours(cairo(2026, 8, 29, 2, 30)))
        self.assertTrue(timing.in_quiet_hours(cairo(2026, 8, 29, 8, 59)))
        self.assertFalse(timing.in_quiet_hours(cairo(2026, 8, 29, 9, 0)))
        self.assertFalse(timing.in_quiet_hours(cairo(2026, 8, 29, 21, 59)))

    def test_the_check_reads_cairo_not_utc(self) -> None:
        """23:00 Cairo is 20:00 or 21:00 UTC - quiet by Cairo's clock, not UTC's."""
        moment = datetime(2026, 8, 28, 20, 30, tzinfo=timezone.utc)
        self.assertTrue(timing.in_quiet_hours(moment))

    def test_a_quiet_nudge_is_moved_to_09_00_not_dropped(self) -> None:
        late = timing.next_allowed(cairo(2026, 8, 28, 23, 30))
        self.assertEqual(late.astimezone(CAIRO), cairo(2026, 8, 29, 9, 0))
        early = timing.next_allowed(cairo(2026, 8, 29, 3, 0))
        self.assertEqual(early.astimezone(CAIRO), cairo(2026, 8, 29, 9, 0))

    def test_an_allowed_moment_is_returned_untouched(self) -> None:
        moment = cairo(2026, 8, 29, 15, 0)
        self.assertEqual(timing.next_allowed(moment), moment)

    def test_compressed_time_has_no_wall_clock_to_be_quiet_in(self) -> None:
        self.assertFalse(timing.in_quiet_hours(cairo(2026, 8, 29, 3, 0), 5))


class TestDayIndex(unittest.TestCase):
    def test_one_cairo_day_is_one_bucket(self) -> None:
        morning = timing.day_index(cairo(2026, 8, 29, 9, 0))
        evening = timing.day_index(cairo(2026, 8, 29, 21, 0))
        tomorrow = timing.day_index(cairo(2026, 8, 30, 9, 0))
        self.assertEqual(morning, evening)
        self.assertEqual(tomorrow, morning + 1)

    def test_a_compressed_day_is_a_scaled_bucket(self) -> None:
        first = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        later = datetime(2026, 8, 29, 12, 0, 5, tzinfo=timezone.utc)
        self.assertNotEqual(timing.day_index(first, 1), timing.day_index(later, 1))
        self.assertEqual(timing.day_index(first, 60), timing.day_index(later, 60))


# --------------------------------------------------------------------------- #
# rev 17, item 10: a date a patient can read
# --------------------------------------------------------------------------- #
class TheDateAPatientReads(unittest.TestCase):
    """"2026-09-01" is not a date to a fifty-year-old in Cairo.

    The weekday is the part he actually uses, because the weekday is what the
    lab is shut until, and hearing it back is the proof that Sanad read
    "المعمل مقفول لحد الأحد" rather than pattern-matched it.
    """

    def at(self, day: int, hour: int = 9) -> datetime:
        return datetime(2026, 8, day, hour, tzinfo=timezone.utc)

    def test_arabic_is_the_weekday_the_day_and_the_month(self) -> None:
        self.assertEqual(timing.in_words(self.at(30), "ar"), "الأحد ٣٠ أغسطس")
        self.assertEqual(timing.in_words(self.at(29), "ar"), "السبت ٢٩ أغسطس")

    def test_english_is_the_same_date_in_english(self) -> None:
        self.assertEqual(timing.in_words(self.at(30), "en"), "Sunday 30 August")
        self.assertEqual(timing.in_words(self.at(29), "en"), "Saturday 29 August")

    def test_it_carries_no_year_and_no_clock(self) -> None:
        for speak in ("ar", "en"):
            with self.subTest(speak=speak):
                said = timing.in_words(self.at(30), speak)
                self.assertNotIn("2026", said)
                self.assertNotIn(":", said)

    def test_the_day_is_the_cairo_day_and_not_the_utc_one(self) -> None:
        """22:30 UTC on Sunday is already Monday in Cairo."""
        late = datetime(2026, 8, 30, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(timing.in_words(late, "en"), "Monday 31 August")

    def test_the_arabic_digits_are_arabic_and_the_emergency_number_is_not(
            self) -> None:
        """Egyptians read both sets. Only the date uses the Arabic-Indic one."""
        said = timing.in_words(self.at(12), "ar")
        self.assertIn("١٢", said)
        self.assertFalse(any(ch.isascii() and ch.isdigit() for ch in said))

    def test_every_month_and_every_weekday_has_a_word(self) -> None:
        self.assertEqual(len(timing.MONTHS_AR), 12)
        self.assertEqual(len(timing.WEEKDAYS_AR), 7)
        for month in range(1, 13):
            with self.subTest(month=month):
                when = datetime(2026, month, 15, 9, tzinfo=timezone.utc)
                self.assertIn(timing.MONTHS_AR[month - 1],
                              timing.in_words(when, "ar"))


if __name__ == "__main__":
    unittest.main()
