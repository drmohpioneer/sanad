"""What happens to a photo is a table, and this is the table's regression suite.

The model says what the picture is. Everything after that is core/photos.py, so
every one of these cases is decided with no model, no database and no network -
which is the point: a judge can read the rule and then read the test that proves
it holds.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from core import photos
from core.models import Loop

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def loop(kind: str, state: str, title: str = "") -> Loop:
    return Loop(id=f"{kind}-{state}-{title}", patient_id="p", doctor_id="d",
                type=kind, title=title or f"{kind} loop", state=state,
                details={"test_name": title} if title else {},
                created_at=NOW, updated_at=NOW)


class Routing(unittest.TestCase):
    def test_a_slip_with_a_matching_order_attaches(self) -> None:
        self.assertEqual(
            photos.route("lab_slip", test_loop=True, monitor_loop=False),
            "attach_to_loop",
        )

    def test_a_slip_with_no_order_is_still_read(self) -> None:
        """The finding from Mohamed's first phone test: read it either way."""
        self.assertEqual(
            photos.route("lab_slip", test_loop=False, monitor_loop=False),
            "unexpected_result",
        )
        self.assertEqual(
            photos.route("lab_slip", test_loop=False, monitor_loop=True),
            "unexpected_result",
        )

    def test_a_monitor_screen_goes_to_the_chart(self) -> None:
        self.assertEqual(
            photos.route("bp_monitor", test_loop=False, monitor_loop=True),
            "monitor_reading",
        )
        self.assertEqual(
            photos.route("bp_monitor", test_loop=True, monitor_loop=False),
            "unfiled_reading",
        )

    def test_everything_else_is_relayed_unread(self) -> None:
        for kind in ("prescription", "other"):
            for test_loop in (True, False):
                with self.subTest(kind=kind, test_loop=test_loop):
                    self.assertEqual(
                        photos.route(kind, test_loop=test_loop, monitor_loop=True),
                        "relay",
                    )


class WhichLoopTakesIt(unittest.TestCase):
    def test_a_late_result_from_an_unreachable_patient_is_welcome(self) -> None:
        """S3 review, carry-over 1: a late result is still the result."""
        self.assertIn("unreachable", photos.OPEN_TEST_STATES)
        found = photos.open_test_loop([loop("TEST", "unreachable")])
        self.assertIsNotNone(found)

    def test_a_closed_loop_does_not_take_it(self) -> None:
        self.assertIsNone(photos.open_test_loop([loop("TEST", "done")]))
        self.assertIsNone(photos.open_test_loop([loop("TEST", "pending_review")]))

    def test_the_oldest_open_loop_wins(self) -> None:
        first, second = loop("TEST", "open"), loop("TEST", "waiting_patient")
        self.assertIs(photos.open_test_loop([first, second]), first)

    def test_a_monitor_loop_is_not_a_test_loop(self) -> None:
        self.assertIsNone(photos.open_test_loop([loop("MONITOR", "open")]))
        self.assertIsNone(photos.open_monitor_loop([loop("TEST", "open")]))


class WhichOfTwoOpenTestsTakesIt(unittest.TestCase):
    """S4 review, carry-over 1. The slip's analytes pick the loop, not the clock.

    The board here is the one in the carry-over: a lipid panel opened first and
    an electrolytes panel opened after it, both waiting for a result.
    """

    def setUp(self) -> None:
        self.lipid = loop("TEST", "open", "Lipid panel")
        self.kidney = loop("TEST", "waiting_patient", "Kidney function tests")
        self.board = [self.lipid, self.kidney]

    def test_a_potassium_slip_goes_to_the_electrolytes_loop(self) -> None:
        found = photos.open_test_loop(
            self.board,
            ["Urea", "Creatinine", "Sodium (Na+)", "Potassium (K+)", "Calcium"],
        )
        self.assertIs(found, self.kidney)

    def test_a_lipid_slip_goes_to_the_lipid_loop(self) -> None:
        found = photos.open_test_loop(
            self.board,
            ["Total Cholesterol", "Triglycerides", "HDL Cholesterol",
             "LDL Cholesterol"],
        )
        self.assertIs(found, self.lipid)

    def test_the_oldest_still_wins_when_the_slip_matches_neither(self) -> None:
        """No overlap is a tie, and a tie is the old rule: oldest open loop."""
        self.assertIs(photos.open_test_loop(self.board, ["TSH", "Free T4"]),
                      self.lipid)

    def test_the_oldest_still_wins_when_no_analytes_are_offered(self) -> None:
        self.assertIs(photos.open_test_loop(self.board), self.lipid)

    def test_one_open_loop_takes_whatever_arrives(self) -> None:
        """A result with no other loop to go to is still that loop's result."""
        self.assertIs(
            photos.open_test_loop([self.lipid], ["Potassium (K+)"]), self.lipid
        )

    def test_the_loop_title_is_read_when_there_is_no_test_name(self) -> None:
        titled = Loop(id="t", patient_id="p", doctor_id="d", type="TEST",
                      title="Kidney function tests", state="open",
                      created_at=NOW, updated_at=NOW)
        self.assertEqual(photos.test_name_of(titled), "Kidney function tests")
        self.assertIs(
            photos.open_test_loop([self.lipid, titled], ["Potassium (K+)"]), titled
        )


class MonitorScreen(unittest.TestCase):
    def test_both_numbers_or_nothing(self) -> None:
        self.assertIsNone(photos.reading_row("128", "", "", NOW))
        self.assertIsNone(photos.reading_row("", "84", "72", NOW))
        self.assertIsNone(photos.reading_row("--", "--", "", NOW))

    def test_a_reading_matches_a_typed_one(self) -> None:
        row = photos.reading_row("128", "84", "", NOW)
        self.assertEqual(row["value"], "128/84")
        self.assertEqual(row["number"], 128.0)
        self.assertEqual(row["at"], "2026-08-29T09:00+00:00")
        self.assertNotIn("pulse", row)

    def test_the_pulse_comes_along_when_the_screen_shows_it(self) -> None:
        row = photos.reading_row("140 mmHg", "90 mmHg", "72 bpm", NOW)
        self.assertEqual(row["value"], "140/90")
        self.assertEqual(row["pulse"], 72)
        self.assertEqual(photos.reading_line(row), "BP 140/90 mmHg, pulse 72")


if __name__ == "__main__":
    unittest.main()
