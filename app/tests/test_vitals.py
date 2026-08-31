"""The blood-pressure table's regression suite.

Every case here runs with no model, no database and no network, because the
table is a table: two numbers in, one verdict out. A judge can read the four
cutoffs in core/vitals.py and then read the tests that hold them in place.

The boundaries are tested from both sides on purpose. "180 or above" and "below
90" are the doctor's own words, so 179 must not be a crisis and 180 must be,
and 90 must not be low while 89 is.
"""

from __future__ import annotations

import unittest

from core import vitals


class TheCutoffs(unittest.TestCase):
    def test_the_table_is_the_numbers_mohamed_gave(self) -> None:
        self.assertEqual(vitals.SYSTOLIC_CRISIS, 180)
        self.assertEqual(vitals.DIASTOLIC_CRISIS, 120)
        self.assertEqual(vitals.SYSTOLIC_LOW, 90)

    def test_systolic_at_or_above_180_is_a_crisis(self) -> None:
        self.assertEqual(vitals.judge(180, 100).level, "crisis")
        self.assertEqual(vitals.judge(190, 95).level, "crisis")

    def test_systolic_just_below_180_is_not(self) -> None:
        self.assertEqual(vitals.judge(179, 100).level, "normal")

    def test_diastolic_at_or_above_120_is_a_crisis_on_its_own(self) -> None:
        """Either number reaching its cutoff is enough; they are not an AND."""
        self.assertEqual(vitals.judge(150, 120).level, "crisis")
        self.assertEqual(vitals.judge(130, 125).level, "crisis")

    def test_diastolic_just_below_120_is_not(self) -> None:
        self.assertEqual(vitals.judge(150, 119).level, "normal")

    def test_systolic_below_90_is_low(self) -> None:
        self.assertEqual(vitals.judge(89, 60).level, "low")
        self.assertEqual(vitals.judge(80, 50).level, "low")

    def test_ninety_is_not_low(self) -> None:
        self.assertEqual(vitals.judge(90, 60).level, "normal")

    def test_an_ordinary_reading_is_filed_and_nothing_else(self) -> None:
        for systolic, diastolic in ((120, 80), (142, 91), (128, 84), (160, 95)):
            with self.subTest(bp=f"{systolic}/{diastolic}"):
                verdict = vitals.judge(systolic, diastolic)
                self.assertEqual(verdict.level, "normal")
                self.assertFalse(verdict.red)
                self.assertFalse(verdict.emergency)


class WhoHearsWhat(unittest.TestCase):
    """Both red rows reach the patient. Decided 2026-08-29, S5 pass 2.

    Pass 1 sent the emergency block for a crisis only and asked whether a low
    reading should send it too. It should: a systolic under 90 measured at home
    is not a reading to sit on. These two assertions are what stops that from
    drifting back.
    """

    def test_a_crisis_reaches_the_patient_and_the_doctor(self) -> None:
        verdict = vitals.judge(190, 125)
        self.assertTrue(verdict.red)
        self.assertTrue(verdict.emergency)

    def test_a_low_reading_reaches_the_patient_and_the_doctor(self) -> None:
        verdict = vitals.judge(85, 55)
        self.assertTrue(verdict.red)
        self.assertTrue(verdict.emergency)

    def test_an_ordinary_reading_reaches_neither(self) -> None:
        verdict = vitals.judge(128, 84)
        self.assertFalse(verdict.red)
        self.assertFalse(verdict.emergency)


class ReadingTheMessage(unittest.TestCase):
    def test_a_bare_reading_is_a_reading(self) -> None:
        self.assertEqual(vitals.parse("185/125"), (185, 125))
        self.assertEqual(vitals.parse("  90 / 60 "), (90, 60))

    def test_a_sentence_with_a_number_in_it_is_not(self) -> None:
        """Prose is the Concierge's business. Only a whole reading is graded."""
        for text in ("my pressure was 190/120 yesterday", "190 over 120",
                     "ضغطي 190/120", "185", "", "--/--"):
            with self.subTest(text=text):
                self.assertIsNone(vitals.parse(text))
                self.assertIsNone(vitals.judge_text(text))

    def test_a_photographed_reading_and_a_typed_one_are_graded_alike(self) -> None:
        """core/photos.reading_row builds "185/125"; this is the same door."""
        self.assertEqual(vitals.judge_text("185/125").level, "crisis")
        self.assertEqual(vitals.judge_text("142/91").level, "normal")


class ReadingASentence(unittest.TestCase):
    """S24 finding 6. A measurement reported in prose is still a measurement.

    `parse` stays anchored, because "is this whole message a reading" is a
    different question and the answer to it decides whether the patient gets
    the fixed acknowledgement. What is new is reading every plausible pressure
    out of a sentence, so the chart and the table both see it.
    """

    def test_two_readings_in_one_sentence_are_both_found(self) -> None:
        self.assertEqual(
            vitals.find_all("Morning BP 148/92, evening 152/95"),
            ((148, 92), (152, 95)))

    def test_a_crisis_in_a_sentence_is_found_and_graded(self) -> None:
        found = vitals.judge_all(
            "My BP this morning was 190/125 and I have a bad headache")
        self.assertEqual(1, len(found))
        self.assertEqual("crisis", found[0].level)
        self.assertEqual("code", found[0].as_meta()["net"])

    def test_arabic_prose_is_read_the_same_way(self) -> None:
        self.assertEqual(vitals.find_all("ضغطي 190/120"), ((190, 120),))

    def test_the_worst_reading_wins_wherever_it_was_written(self) -> None:
        worst = vitals.worst(vitals.judge_all("140/85 then 195/130"))
        self.assertEqual((195, 130), (worst.systolic, worst.diastolic))

    def test_two_readings_of_the_same_severity_keep_the_order_written(self
                                                                     ) -> None:
        worst = vitals.worst(vitals.judge_all("148/92 then 152/95"))
        self.assertEqual((148, 92), (worst.systolic, worst.diastolic))

    def test_nothing_to_read_is_no_reading(self) -> None:
        self.assertEqual((), vitals.find_all("I feel fine today"))
        self.assertIsNone(vitals.worst(()))

    def test_what_the_plausibility_guard_refuses(self) -> None:
        """A fraction, a date, a time, a score. None of them is a pressure."""
        for text in ("I took 1/2 tablet", "collected on 28/08/2026",
                     "the appointment is at 10/30", "3/4 of the dose",
                     "my sugar was 90/300", "reference 12/2026"):
            with self.subTest(text=text):
                self.assertEqual((), vitals.find_all(text))

    def test_the_bounds_are_the_ones_the_guard_names(self) -> None:
        self.assertTrue(vitals.plausible(60, 30))
        self.assertTrue(vitals.plausible(300, 200))
        self.assertFalse(vitals.plausible(59, 30))
        self.assertFalse(vitals.plausible(301, 200))
        self.assertFalse(vitals.plausible(120, 29))
        self.assertFalse(vitals.plausible(120, 201))
        # A diastolic at or above the systolic is not a reading either way up.
        self.assertFalse(vitals.plausible(90, 90))
        self.assertFalse(vitals.plausible(90, 120))

    def test_a_pressure_is_never_read_out_of_a_longer_run_of_digits(self) -> None:
        self.assertEqual((), vitals.find_all("serial 1148/9224"))
        self.assertEqual((), vitals.find_all("ref 4148/92"))

    def test_the_anchored_door_is_unchanged(self) -> None:
        """`parse` still answers only for a message that is nothing else."""
        self.assertIsNone(vitals.parse("Morning BP 148/92, evening 152/95"))
        self.assertIsNone(vitals.judge_text("my pressure was 190/120 yesterday"))
        self.assertEqual((148, 92), vitals.parse("148/92"))


class WhatTheDoctorSees(unittest.TestCase):
    def test_the_card_names_the_file_that_decided(self) -> None:
        card = vitals.red_card("Hend Ismail", vitals.judge(190, 125))
        self.assertEqual(card["severity"], "red")
        self.assertIn("Hend Ismail", card["title"])
        self.assertIn("BP 190/125 mmHg", card["lines"][0])
        self.assertIn("core/vitals.py", " ".join(card["lines"]))
        self.assertIn("decided_by", " ".join(card["lines"]))

    def test_the_card_carries_the_lines_its_caller_adds(self) -> None:
        card = vitals.red_card(
            "Hend Ismail", vitals.judge(85, 55), ["Added to Blood pressure chart."]
        )
        self.assertIn("Added to Blood pressure chart.", card["lines"])

    def test_the_audit_meta_matches_the_sentinel_shape(self) -> None:
        """The console reads one shape for every escalation on a card."""
        meta = vitals.judge(190, 125).as_meta()
        for key in ("fired", "net", "concept", "nets_run"):
            self.assertIn(key, meta)
        self.assertEqual(meta["net"], "code")
        self.assertEqual(meta["nets_run"], ["code"])
        self.assertTrue(meta["fired"])
        self.assertFalse(vitals.judge(120, 80).as_meta()["fired"])


if __name__ == "__main__":
    unittest.main()
