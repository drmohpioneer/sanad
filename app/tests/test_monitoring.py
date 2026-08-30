"""The monitoring summary, in the exact shape S6++ item H writes it.

The seeded case is the spec's own: blood pressure twice a day for seven days,
fourteen expected readings, eleven received, the evenings of days 3, 5 and 6
never sent. If the sentence below ever changes shape, this file fails, which is
the point of asserting it word for word rather than field by field.

Pure: no model, no database, no network.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from core import monitoring

# 2026-08-24 00:00 Cairo is 2026-08-23 21:00 UTC. The loop is created at 08:00
# Cairo on day one, which is where the seven days are counted from.
START = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)  # 08:00 Cairo


class Loop:
    """The three fields core/monitoring.py reads, and nothing else."""

    def __init__(self, readings, *, schedule="twice a day", days=7,
                 metric="BP", barrier="", created_at=START, kind="MONITOR"):
        self.type = kind
        self.title = "Blood pressure monitoring"
        self.details = {"metric": metric, "schedule": schedule, "days": days}
        self.readings = readings
        self.barrier = barrier
        self.created_at = created_at


def reading(day: int, hour: int, systolic: int, diastolic: int = 85) -> dict:  # noqa: E501
    """One reading, at that hour Cairo on that day of the schedule.

    Day one is the day the doctor confirms the plan. The plan is the first ask;
    reminders begin on day two.
    """
    when = (START.astimezone(monitoring.timing.CAIRO)
            .replace(hour=hour, minute=0)
            + timedelta(days=day - 1))
    return {"at": when.astimezone(timezone.utc).isoformat(timespec="minutes"),
            "value": f"{systolic}/{diastolic}", "number": float(systolic)}


def seeded() -> Loop:
    """Fourteen slots, eleven readings, three gaps: the spec's own example.

    Both numbers fall, because a blood pressure is a pair and rev 18 item c
    reads it as one: a fixture whose diastolic never moved could not tell a
    trend that carries it from a trend that repeats a constant.
    """
    rows = []
    falling = {1: 158, 2: 154, 3: 150, 4: 146, 5: 142, 6: 138, 7: 134}
    lower = {1: 92, 2: 91, 3: 90, 4: 89, 5: 88, 6: 87, 7: 86}
    for day in range(1, 8):
        rows.append(reading(day, 8, falling[day] + 4, lower[day] + 2))
        if day in (3, 5, 6):
            continue                                          # evening missing
        rows.append(reading(day, 20, falling[day], lower[day]))
    return Loop(rows)


class TheSeededWeek(unittest.TestCase):
    def test_the_whole_line_is_the_shape_the_spec_fixes(self) -> None:
        summary = monitoring.summary(seeded())
        self.assertEqual(
            summary.line,
            "Requested: BP twice daily for seven days · "
            "Expected readings: 14 · Received: 11 · "
            "Missing: evenings on days 3, 5 and 6 · "
            "Trend: 159/93 to 138/88 · "
            "Threshold alerts: 0 · "
            "Patient-reported barrier: none",
        )

    def test_the_counts_are_the_ones_a_doctor_can_take_literally(self) -> None:
        summary = monitoring.summary(seeded())
        self.assertEqual(summary.expected, 14)
        self.assertEqual(summary.received, 11)
        self.assertEqual(summary.missing_slots, ((3, 1), (5, 1), (6, 1)))

    def test_the_trend_is_the_first_three_against_the_last_three(self) -> None:
        summary = monitoring.summary(seeded())
        # Mornings and evenings interleave, so the first three readings are
        # 162/94, 158/92 and 158/93, and the last three are 142/89, 138/88 and
        # 134/86. Both averages are rounded half up, because a blood pressure
        # is read in whole numbers.
        self.assertEqual(summary.trend, "159/93 to 138/88")

    def test_a_metric_that_is_not_a_pair_keeps_its_single_number(self) -> None:
        """A daily weight has no second number and none is invented for it."""
        rows = [{"value": str(kg), "number": float(kg)}
                for kg in (86, 85, 84, 83, 82)]
        self.assertEqual(monitoring.trend_of(rows), "85 to 83")

    def test_one_reading_without_a_pair_takes_the_whole_trend_back(self) -> None:
        """An average over three readings of which one has no diastolic is not
        the average of anything, so the whole trend falls back."""
        rows = [{"value": "150/90", "number": 150.0},
                {"value": "148", "number": 148.0},
                {"value": "146/88", "number": 146.0}]
        self.assertEqual(monitoring.trend_of(rows), "too early")

    def test_a_barrier_the_patient_reported_is_named(self) -> None:
        loop = seeded()
        loop.barrier = "forgot"
        self.assertIn("Patient-reported barrier: forgot",
                      monitoring.summary(loop).line)

    def test_a_reading_the_table_calls_red_is_a_threshold_alert(self) -> None:
        loop = seeded()
        loop.readings = [*loop.readings, reading(7, 21, 190, 125)]
        summary = monitoring.summary(loop)
        self.assertEqual(summary.alerts, 1)
        self.assertIn("Threshold alerts: 1", summary.line)


class TheEdgesOfTheWeek(unittest.TestCase):
    def test_a_full_week_is_missing_nothing(self) -> None:
        rows = []
        for day in range(1, 8):
            rows += [reading(day, 8, 150), reading(day, 20, 145)]
        summary = monitoring.summary(Loop(rows))
        self.assertEqual(summary.received, 14)
        self.assertEqual(summary.missing, "none")

    def test_a_week_with_nothing_at_all_says_so_in_one_phrase(self) -> None:
        summary = monitoring.summary(Loop([]))
        self.assertEqual(summary.missing, "all 14 readings")
        self.assertEqual(summary.trend, "not enough readings")

    def test_a_whole_day_missing_is_named_as_a_day(self) -> None:
        rows = []
        for day in (1, 2, 3, 5, 6, 7):
            rows += [reading(day, 8, 150), reading(day, 20, 145)]
        summary = monitoring.summary(Loop(rows))
        self.assertEqual(summary.missing, "every reading on day 4")

    def test_one_reading_is_a_direction_nobody_can_call_a_trend(self) -> None:
        self.assertEqual(monitoring.summary(Loop([reading(1, 8, 150)])).trend,
                         "not enough readings")

    def test_a_reading_outside_the_week_is_received_but_fills_no_slot(self) -> None:
        rows = [reading(1, 8, 150), reading(30, 8, 150)]
        summary = monitoring.summary(Loop(rows))
        self.assertEqual(summary.received, 2)
        self.assertIn((1, 1), summary.missing_slots)

    def test_a_reading_with_no_timestamp_is_counted_and_placed_nowhere(self) -> None:
        summary = monitoring.summary(Loop([{"value": "150/90", "number": 150.0}]))
        self.assertEqual(summary.received, 1)
        self.assertEqual(len(summary.missing_slots), 14)


class TheScheduleTheDoctorDictated(unittest.TestCase):
    def test_the_words_a_doctor_uses_become_a_number(self) -> None:
        for said, number in (("twice a day", 2), ("twice daily", 2),
                             ("مرتين في اليوم", 2), ("three times a day", 3),
                             ("once a day", 1), ("daily", 1),
                             ("every day", 1), ("four times a day", 4)):
            with self.subTest(said=said):
                self.assertEqual(monitoring.per_day(said), number)

    def test_a_schedule_it_cannot_read_is_once_a_day(self) -> None:
        """The reading it can never call missing wrongly."""
        for said in ("", "when he remembers", "as needed"):
            with self.subTest(said=said):
                self.assertEqual(monitoring.per_day(said), 1)

    def test_once_a_day_has_one_slot_and_names_it_readings(self) -> None:
        rows = [reading(day, 9, 150) for day in (1, 2, 4, 5)]
        summary = monitoring.summary(Loop(rows, schedule="once a day", days=5))
        self.assertEqual(summary.expected, 5)
        self.assertEqual(summary.missing, "every reading on day 3")

    def test_three_a_day_splits_morning_afternoon_and_evening(self) -> None:
        self.assertEqual(monitoring.slot_of(9, 3), 0)
        self.assertEqual(monitoring.slot_of(13, 3), 1)
        self.assertEqual(monitoring.slot_of(20, 3), 2)

    def test_seven_days_is_written_as_a_word(self) -> None:
        self.assertEqual(monitoring.days_word(7), "seven")
        self.assertEqual(monitoring.days_word(14), "fourteen")
        self.assertEqual(monitoring.days_word(30), "thirty")
        self.assertEqual(monitoring.days_word(45), "45")

    def test_one_day_is_not_written_as_days(self) -> None:
        summary = monitoring.summary(Loop([], schedule="once a day", days=1))
        self.assertIn("for one day ·", summary.line)


class TheWindowStartsWhenTheDoctorAsks(unittest.TestCase):
    """Confirm and the welcome carry day one's ask; reminders cover days 2..N."""

    def asked_on(self, day: int, hour: int = 9, systolic: int = 130) -> dict:
        """A reading sent on this numbered day of the monitoring contract."""
        when = (START.astimezone(monitoring.timing.CAIRO)
                .replace(hour=hour, minute=0) + timedelta(days=day - 1))
        return {"at": when.astimezone(timezone.utc).isoformat(timespec="minutes"),
                "value": f"{systolic}/85", "number": float(systolic)}

    def test_a_two_day_monitor_answered_on_both_days_is_missing_nothing(self) -> None:
        loop = Loop([self.asked_on(1), self.asked_on(2)],
                    schedule="once a day", days=2)
        summary = monitoring.summary(loop)
        self.assertEqual(summary.expected, 2)
        self.assertEqual(summary.received, 2)
        self.assertEqual(summary.missing_slots, ())
        self.assertEqual(summary.missing, "none")

    def test_the_last_contract_day_is_inside_the_window(self) -> None:
        rows = [self.asked_on(day) for day in range(1, 8)]
        summary = monitoring.summary(Loop(rows, schedule="once a day", days=7))
        self.assertEqual(summary.received, 7)
        self.assertEqual(summary.missing_slots, ())

    def test_the_day_a_reading_is_missed_is_named_by_contract_day(self) -> None:
        rows = [self.asked_on(1), self.asked_on(3)]
        summary = monitoring.summary(Loop(rows, schedule="once a day", days=3))
        self.assertEqual(summary.missing_slots, ((2, 0),))
        self.assertEqual(summary.missing, "every reading on day 2")

    def test_a_reading_on_confirm_day_fills_day_one(self) -> None:
        loop = Loop([self.asked_on(1), self.asked_on(2), self.asked_on(3)],
                    schedule="once a day", days=2)
        summary = monitoring.summary(loop)
        self.assertEqual(summary.received, 3)
        self.assertEqual(summary.missing_slots, ())


class TheDemoScaleIsADayToo(unittest.TestCase):
    """S11 wave A round 2, kernel review F11 and F12.

    docs/RUNBOOK.md runs the rehearsal at `time_scale=3`: three real seconds are
    one Sanad day, so the whole ladder plays out while the judges watch. The
    chaser honours that (`timing.seconds(day, scale)`), and after round 1 the
    monitoring summary did not: it placed every reading by the Cairo CALENDAR
    date, and at any compressed scale every reading of the run falls on the same
    calendar date. The reviewer traced it at scale 60: two readings answered
    five seconds after each reminder printed "Received: 2" and "Missing: every
    reading on days 1 and 2" in the same sentence.

    `timing.day_index` already buckets an instant correctly at both scales, and
    the summary now uses it.
    """

    SCALE = 3

    def loop_at(self, scale: int, offsets, *, days=2, schedule="once a day"):
        """A loop created at START with one reading at each offset in seconds."""
        rows = [
            {"at": (START + timedelta(seconds=off)).isoformat(timespec="seconds"),
             "value": f"{130 + i}/85", "number": float(130 + i)}
            for i, off in enumerate(offsets)
        ]
        return Loop(rows, schedule=schedule, days=days)

    def test_a_compressed_run_answered_on_every_reminder_is_missing_nothing(self) -> None:
        """Confirm asks on day one; the reminder asks again on day two."""
        loop = self.loop_at(self.SCALE, [1, self.SCALE + 1])
        summary = monitoring.summary(loop, time_scale=self.SCALE)
        self.assertEqual(summary.received, 2)
        self.assertEqual(summary.missing_slots, ())
        self.assertEqual(summary.missing, "none")

    def test_the_reviewers_scale_sixty_reproduction(self) -> None:
        loop = self.loop_at(60, [5, 65])
        summary = monitoring.summary(loop, time_scale=60)
        self.assertEqual(summary.expected, 2)
        self.assertEqual(summary.received, 2)
        self.assertEqual(summary.missing, "none")

    def test_a_compressed_run_still_reports_the_day_that_was_skipped(self) -> None:
        loop = self.loop_at(self.SCALE, [1], days=2)
        summary = monitoring.summary(loop, time_scale=self.SCALE)
        self.assertEqual(summary.missing_slots, ((2, 0),))

    def test_twice_a_day_splits_the_compressed_day_into_two_slots(self) -> None:
        """A slot is a position inside the day, and at demo scale a day is three
        seconds, so the hour of the clock cannot be what names it."""
        loop = self.loop_at(
            self.SCALE, [0, 2, self.SCALE, self.SCALE + 2],
            schedule="twice a day",
        )
        summary = monitoring.summary(loop, time_scale=self.SCALE)
        self.assertEqual(summary.expected, 4)
        self.assertEqual(summary.missing_slots, ())

    def test_real_time_is_untouched_by_the_scale_argument(self) -> None:
        """The default is real time and the seeded week is bit for bit the same."""
        self.assertEqual(monitoring.summary(seeded()).line,
                         monitoring.summary(seeded(), time_scale=86400).line)
        self.assertEqual(monitoring.summary(seeded()).missing_slots,
                         ((3, 1), (5, 1), (6, 1)))


class WhereItIsShown(unittest.TestCase):
    """main.py cannot be imported without FastAPI, so the wiring is read."""

    def setUp(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        self.main = (root / "main.py").read_text(encoding="utf-8")
        self.report = (root / "core" / "report.py").read_text(encoding="utf-8")

    def test_the_patient_view_carries_it_for_every_monitoring_loop(self) -> None:
        view = self.main.split("async def patient_view(", 1)[1].split(
            "@app.get", 1)[0]
        self.assertIn('"monitoring": [', view)
        self.assertIn("monitoring.summary(l, time_scale).as_dict()", view)
        self.assertIn("monitoring.is_monitoring(l)", view)

    def test_the_patient_view_reads_the_live_time_scale_first(self) -> None:
        """wave A F11: the panel counts days, so it needs the rehearsal's day."""
        view = self.main.split("async def patient_view(", 1)[1].split(
            "@app.get", 1)[0]
        self.assertIn("_, time_scale = await settings.current()", view)
        self.assertLess(view.index("settings.current()"),
                        view.index("monitoring.summary(l, time_scale)"))

    def test_the_completion_report_prints_it_instead_of_the_bare_trend(self) -> None:
        block = self.report.split("def _loop_block(", 1)[1].split(
            "def trend(", 1)[0]
        self.assertIn("monitoring.is_monitoring(loop)", block)
        self.assertIn("monitoring.line(loop, time_scale)", block)
        self.assertLess(block.index("monitoring.line(loop, time_scale)"),
                        block.index("trend(readings)"))

    def test_the_report_reads_the_live_time_scale_and_hands_it_down(self) -> None:
        """wave A F11, the other half: report.build cannot reach settings from
        inside a synchronous _loop_block, so it reads it once and passes it."""
        build = self.report.split("async def build(", 1)[1].split(
            "def _summary(", 1)[0]
        self.assertIn("_, time_scale = await settings.current()", build)
        self.assertIn("_loop_block(loop, time_scale)", build)


class ItSaysNothingAboutAnyOtherKindOfLoop(unittest.TestCase):
    def test_a_test_loop_gets_no_monitoring_line(self) -> None:
        loop = Loop([], kind="TEST")
        self.assertFalse(monitoring.is_monitoring(loop))
        self.assertEqual(monitoring.line(loop), "")

    def test_the_line_has_no_dash_a_machine_would_have_written(self) -> None:
        line = monitoring.summary(seeded()).line
        self.assertNotIn("—", line)
        self.assertNotIn("–", line)


if __name__ == "__main__":
    unittest.main()
