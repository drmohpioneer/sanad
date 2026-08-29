"""The three checks a slip has to pass before it satisfies a contract.

S6+ item B. Until this existed a photograph that reached the extractor was
attached on the strength of its analytes alone, so somebody else's slip, or one
collected before the doctor had ordered anything, closed a loop as neatly as the
right one.

The last class here is the S5 pass-2 carry-over: a synthetic slip whose unit
cannot be converted and whose flagged row has no table entry, proved to reach
urgent review rather than the ordinary pile.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from core import labs, names, verify

ORDERED = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


def loop(test_name: str = "Kidney function tests", **details):
    return SimpleNamespace(
        id="l", type="TEST", title=test_name,
        details={"test_name": test_name, **details},
        created_at=ORDERED, due_at=ORDERED + timedelta(days=14), state="open",
    )


class TheNameOnTheSlip(unittest.TestCase):
    """Fuzzy on purpose, in both scripts, because labs print what they like."""

    def test_a_title_and_an_extra_name_part_are_still_the_same_person(self) -> None:
        for printed in ("Mr. Ahmed Ali", "AHMED ALI HASSAN", "Ahmed  Ali",
                        "Patient: Ahmed Ali"):
            with self.subTest(printed=printed):
                same, _ = names.same_person(printed, "Ahmed Ali")
                self.assertTrue(same)

    def test_arabic_spelling_variants_are_the_same_person(self) -> None:
        same, _ = names.same_person("احمد على حسن", "أحمد علي")
        self.assertTrue(same)

    def test_a_different_person_is_a_different_person(self) -> None:
        same, _ = names.same_person("Mohamed Sayed", "Ahmed Ali")
        self.assertFalse(same)

    def test_a_mismatch_never_attaches(self) -> None:
        verdict = verify.check(
            printed_name="Mohamed Sayed", printed_date="2026-08-21",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.identity, "mismatch")
        self.assertTrue(verdict.identity_failed)
        self.assertFalse(verdict.attaches)
        self.assertFalse(verdict.satisfies)

    def test_two_alphabets_are_cannot_compare_not_a_mismatch(self) -> None:
        """The doctor is told the truth: nothing was compared, not that this is
        somebody else's result."""
        verdict = verify.check(
            printed_name="أحمد علي", printed_date="2026-08-21",
            printed_analytes=["Urea"], patient_name="Ahmed Ali",
            ordered_on=ORDERED,
        )
        self.assertEqual(verdict.identity, "cannot_compare")
        self.assertTrue(verdict.identity_failed)

    def test_a_slip_with_no_name_printed_attaches_but_never_satisfies(self) -> None:
        """S11 wave A item 3, from reviews/codex-troubleshoot-1.md line 4:

        "HIGH Undated or unnamed evidence can satisfy a contract:
        verify.satisfies rejects only before_order; not_printed passes
        (verify.py:156,167; test_verify.py:73 permits missing name)."

        Most Egyptian lab slips print a name and some do not, so refusing those
        would refuse most real results: the values still attach and the doctor
        still sees them. What they cannot do is close the contract, because the
        check that this is his patient's result was never made.
        """
        verdict = verify.check(
            printed_name="", printed_date="2026-08-21",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.identity, "not_printed")
        self.assertTrue(verdict.attaches)
        self.assertFalse(verdict.satisfies)
        self.assertTrue(any("prints no name" in r for r in verdict.reasons))
        self.assertIn("identity", verdict.unverified)
        self.assertTrue(any("could not be done" in line for line in verdict.lines()))

    def test_a_slip_with_no_date_printed_attaches_but_never_satisfies(self) -> None:
        """The other half of the same claim: an undated slip is not evidence
        that the test was done after the doctor asked for it."""
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="see comment",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.dated, "not_printed")
        self.assertTrue(verdict.attaches)
        self.assertFalse(verdict.satisfies)
        self.assertIn("date", verdict.unverified)
        self.assertTrue(any("could not be done" in line for line in verdict.lines()))

    def test_a_slip_with_neither_names_both_checks_on_the_card(self) -> None:
        verdict = verify.check(
            printed_name="", printed_date="",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertTrue(verdict.attaches)
        self.assertFalse(verdict.satisfies)
        self.assertEqual(verdict.unverified, ("identity", "date"))
        line = [l for l in verdict.lines() if "could not be done" in l][0]
        self.assertIn("identity", line)
        self.assertIn("date", line)

    def test_all_three_checks_passing_is_the_only_way_to_satisfy(self) -> None:
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="2026-08-21",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.identity, "match")
        self.assertEqual(verdict.dated, "ok")
        self.assertTrue(verdict.complete)
        self.assertTrue(verdict.satisfies)
        self.assertEqual(verdict.unverified, ())


class TheDateOnTheSlip(unittest.TestCase):
    def test_the_shapes_an_egyptian_lab_prints(self) -> None:
        for printed in ("2026-08-21", "21/08/2026", "21-8-26", "21 Aug 2026",
                        "٢١/٠٨/٢٠٢٦"):
            with self.subTest(printed=printed):
                self.assertEqual(verify.parse_date(printed), date(2026, 8, 21))

    def test_an_american_order_is_read_when_the_day_says_so(self) -> None:
        self.assertEqual(verify.parse_date("08/21/2026"), date(2026, 8, 21))

    def test_anything_unreadable_is_none_rather_than_a_guess(self) -> None:
        for printed in ("see comment", "", None, "not printed"):
            with self.subTest(printed=printed):
                self.assertIsNone(verify.parse_date(printed))

    def test_a_result_collected_before_the_order_does_not_satisfy(self) -> None:
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="2026-08-01",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.dated, "before_order")
        self.assertTrue(verdict.attaches)   # the values are still real
        self.assertFalse(verdict.satisfies)  # the contract stays open

    def test_the_order_day_itself_counts(self) -> None:
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="2026-08-20",
            printed_analytes=["Urea", "Creatinine", "Sodium", "Potassium"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.dated, "ok")


class EveryAnalyteTheDoctorAskedFor(unittest.TestCase):
    def test_a_panel_knows_what_it_is_made_of(self) -> None:
        self.assertEqual(verify.required_analytes(loop("Kidney function tests")),
                         ("Urea", "Creatinine", "Sodium", "Potassium"))
        self.assertEqual(verify.required_analytes(loop("Lipid panel")),
                         ("Total cholesterol", "Triglycerides", "HDL", "LDL"))

    def test_a_doctor_who_named_the_analytes_gets_his_own_list(self) -> None:
        named = loop("whatever", analytes=["Potassium", "Creatinine"])
        self.assertEqual(verify.required_analytes(named),
                         ("Potassium", "Creatinine"))

    def test_a_panel_nobody_knows_asks_for_nothing_rather_than_guessing(self) -> None:
        self.assertEqual(verify.required_analytes(loop("Vitamin D")), ())

    def test_a_partial_result_keeps_the_contract_open_and_names_the_gap(self) -> None:
        """"Creatinine present, potassium missing" is the spec's own example."""
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="2026-08-21",
            printed_analytes=["Urea", "Creatinine"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.missing, ("Sodium", "Potassium"))
        self.assertFalse(verdict.complete)
        self.assertFalse(verdict.satisfies)
        self.assertTrue(verdict.attaches)

    def test_the_spelling_on_the_slip_does_not_decide_completeness(self) -> None:
        verdict = verify.check(
            printed_name="Ahmed Ali", printed_date="2026-08-21",
            printed_analytes=["Blood Urea", "Serum Creatinine (Cr)",
                              "Sodium (Na+)", "Potassium (K+)"],
            patient_name="Ahmed Ali", ordered_on=ORDERED,
            required=verify.required_analytes(loop()),
        )
        self.assertEqual(verdict.missing, ())
        self.assertTrue(verdict.satisfies)


class TheSlipPicksItsOwnLoop(unittest.TestCase):
    """S6 replaces the title-word tie-break with analyte-level matching.

    The old rule counted words two names shared, so a lab that printed nothing
    but "K" and "Na" overlapped a kidney loop by zero.
    """

    def test_an_electrolyte_slip_matches_the_kidney_loop_by_its_analytes(self) -> None:
        rows = ["K", "Na", "Creatinine"]
        self.assertGreater(labs.panel_overlap("Kidney function tests", rows), 0)
        self.assertEqual(labs.panel_overlap("Lipid panel", rows), 0)

    def test_a_lipid_slip_matches_the_lipid_loop(self) -> None:
        rows = ["Total Cholesterol", "Triglycerides", "HDL Cholesterol", "LDL"]
        self.assertEqual(labs.panel_overlap("Lipid panel", rows), 4)
        self.assertEqual(labs.panel_overlap("Kidney function tests", rows), 0)

    def test_the_count_is_analytes_now_and_not_title_words(self) -> None:
        one = labs.panel_overlap("Kidney function tests", ["Creatinine"])
        three = labs.panel_overlap("Kidney function tests",
                                   ["Creatinine", "Urea", "Potassium"])
        self.assertEqual((one, three), (1, 3))


class TheUnjudgeableSlip(unittest.TestCase):
    """S5 pass-2 carry-over: a synthetic slip that has to reach urgent review.

    Two rows, two different ways of being unjudgeable: a haemoglobin printed in
    a unit nothing can convert, and an analyte with no row in the table that the
    lab itself flagged HH. Both must be urgent, not quiet. The slip itself is
    docs/seed/lab-slip-6-unjudgeable.png, so the same case can be photographed
    into the deployed extractor by hand.
    """

    # Row for row, what docs/seed/lab-slip-6-unjudgeable.png prints.
    SLIP = [
        {"analyte": "Haemoglobin", "value": "45", "unit": "%",
         "ref_range": "11.5 - 16.5", "flag": ""},
        {"analyte": "Ferritin", "value": "2450", "unit": "ng/mL",
         "ref_range": "30 - 400", "flag": "HH"},
        {"analyte": "Serum Creatinine", "value": "1.0", "unit": "mg/dL",
         "ref_range": "0.7 - 1.3", "flag": ""},
        {"analyte": "Sodium (Na+)", "value": "139", "unit": "mmol/L",
         "ref_range": "135 - 145", "flag": ""},
        {"analyte": "Potassium (K+)", "value": "4.3", "unit": "mmol/L",
         "ref_range": "3.5 - 5.1", "flag": ""},
    ]

    def test_both_rows_reach_urgent_review_and_the_normal_one_does_not(self) -> None:
        findings = labs.assess(self.SLIP)
        levels = {f.analyte: f.level for f in findings}
        self.assertEqual(levels["Haemoglobin"], "urgent_review")
        self.assertEqual(levels["Ferritin"], "urgent_review")
        self.assertEqual(levels["Serum Creatinine"], "normal")
        self.assertEqual(levels["Potassium (K+)"], "normal")

    def test_the_card_collects_them_and_says_why(self) -> None:
        urgent = labs.urgents(labs.assess(self.SLIP))
        self.assertEqual([f.analyte for f in urgent],
                         ["Haemoglobin", "Ferritin"])
        self.assertTrue(any("cannot be judged in code" in f.line for f in urgent))

    def test_the_two_unjudgeable_rows_are_never_reported_as_normal(self) -> None:
        for finding in labs.assess(self.SLIP):
            if finding.analyte in ("Haemoglobin", "Ferritin"):
                with self.subTest(analyte=finding.analyte):
                    self.assertNotEqual(finding.level, "normal")
                    self.assertTrue(finding.urgent)


if __name__ == "__main__":
    unittest.main()
