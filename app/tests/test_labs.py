"""The critical-lab table and every comparison built on it.

These are the rails under S3's determinism claim: if a threshold moves, an alias
stops matching, or a cutoff-relative analyte starts guessing, the image does not
build (see the Dockerfile).
"""

from __future__ import annotations

import unittest

from core import labs


class TestNames(unittest.TestCase):
    """A lab prints what it likes; the table still has to find its row."""

    def test_aliases_reach_the_table(self) -> None:
        for written, expected in (
            ("Potassium", "K"), ("Serum Potassium (K+)", "K"), ("K", "K"),
            ("LDL-C", "LDL"), ("LDL Cholesterol", "LDL"),
            ("Serum Creatinine", "Creatinine"), ("Troponin I", "Troponin"),
            ("D-Dimer", "D-dimer"), ("Haemoglobin", "Hb"), ("TLC", "WBC"),
        ):
            rule = labs.rule_for(written)
            self.assertIsNotNone(rule, written)
            self.assertEqual(rule.analyte, expected, written)

    def test_an_unknown_analyte_is_never_graded(self) -> None:
        self.assertIsNone(labs.rule_for("HDL"))
        self.assertEqual(labs.judge("HDL", 40.0), "cannot_judge")


class TestParsing(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(labs.parse_value("6.4"), 6.4)
        self.assertEqual(labs.parse_value("6,4"), 6.4)
        self.assertEqual(labs.parse_value("160 mg/dL"), 160.0)
        self.assertIsNone(labs.parse_value("positive"))
        self.assertIsNone(labs.parse_value(""))
        self.assertIsNone(labs.parse_value(None))

    def test_printed_reference_ranges(self) -> None:
        self.assertEqual(labs.parse_range("3.5 - 5.1"), (3.5, 5.1))
        self.assertEqual(labs.parse_range("70-100 mg/dL"), (70.0, 100.0))
        self.assertEqual(labs.parse_range("< 0.04"), (None, 0.04))
        self.assertEqual(labs.parse_range("up to 500"), (None, 500.0))
        self.assertEqual(labs.parse_range("> 12"), (12.0, None))
        self.assertEqual(labs.parse_range("see comment"), (None, None))
        self.assertEqual(labs.parse_range(""), (None, None))


class TestJudge(unittest.TestCase):
    def test_potassium_crosses_the_table(self) -> None:
        self.assertEqual(labs.judge("K", 6.4), "critical")
        self.assertEqual(labs.judge("K", 2.4), "critical")
        self.assertEqual(labs.judge("K", 4.2), "normal")
        self.assertEqual(labs.judge("K", 6.0), "normal")  # the rule is > 6.0

    def test_ldl_never_escalates(self) -> None:
        self.assertEqual(labs.judge("LDL", 160.0, 70.0), "above_target")
        self.assertEqual(labs.judge("LDL", 160.0), "normal")
        self.assertEqual(labs.judge("LDL", 60.0, 70.0), "normal")

    def test_creatinine_against_the_patients_own_baseline(self) -> None:
        self.assertEqual(labs.judge("Creatinine", 2.2, baseline=1.0), "critical")
        self.assertEqual(labs.judge("Creatinine", 1.2, baseline=1.0), "normal")
        self.assertEqual(labs.judge("Creatinine", 4.5), "critical")

    def test_a_value_that_is_not_a_number_is_never_graded(self) -> None:
        self.assertEqual(labs.judge("K", None), "cannot_judge")

    def test_cutoff_relative_analytes_use_the_slip_and_nothing_else(self) -> None:
        # No printed reference, no flag: the spec's exact rule.
        self.assertEqual(labs.judge("Troponin", 0.9), "cannot_judge")
        self.assertEqual(labs.judge("D-dimer", 1200.0), "cannot_judge")
        # The slip's own printed cutoff.
        self.assertEqual(
            labs.judge("Troponin", 0.9, ref_range="< 0.04"), "critical"
        )
        self.assertEqual(
            labs.judge("Troponin", 0.01, ref_range="< 0.04"), "cannot_judge"
        )
        # The slip's own flag, even with no number at all.
        self.assertEqual(labs.judge("D-dimer", None, flag="POSITIVE"), "critical")
        self.assertEqual(labs.judge("Troponin", None, flag="H"), "critical")

    def test_a_flag_never_overrules_the_table(self) -> None:
        """A lab that flags 4.2 mmol/L potassium high is still not an emergency."""
        self.assertEqual(labs.judge("K", 4.2, flag="H"), "normal")


class TestAssess(unittest.TestCase):
    """The whole slip, the way the doctor's card sees it."""

    SLIP = [
        {"analyte": "LDL", "value": "160", "unit": "mg/dL", "ref_range": "< 100",
         "flag": "H"},
        {"analyte": "HDL", "value": "40", "unit": "mg/dL", "ref_range": "> 40",
         "flag": ""},
        {"analyte": "Creatinine", "value": "1.0", "unit": "mg/dL",
         "ref_range": "0.7 - 1.3", "flag": ""},
        {"analyte": "Potassium", "value": "4.2", "unit": "mmol/L",
         "ref_range": "3.5 - 5.1", "flag": ""},
    ]

    def test_the_demo_slip(self) -> None:
        findings = labs.assess(self.SLIP, targets={"LDL": "70"}, baseline={})
        by_name = {f.analyte: f for f in findings}
        self.assertEqual(by_name["LDL"].level, "above_target")
        self.assertEqual(by_name["LDL"].line, "LDL 160 mg/dL, target 70, above target")
        self.assertEqual(by_name["Potassium"].level, "normal")
        self.assertEqual(by_name["HDL"].level, "cannot_judge")
        self.assertEqual(labs.criticals(findings), [])

    def test_a_critical_row_is_found_in_code(self) -> None:
        slip = [{"analyte": "Potassium", "value": "6.4", "unit": "mmol/L",
                 "ref_range": "3.5 - 5.1", "flag": "H"}]
        findings = labs.assess(slip)
        self.assertEqual(len(labs.criticals(findings)), 1)
        self.assertIn("CRITICAL", findings[0].line)

    def test_targets_are_matched_through_aliases(self) -> None:
        """The doctor dictated "LDL"; the lab printed "LDL-C". Same target."""
        slip = [{"analyte": "LDL-C", "value": "160", "unit": "mg/dL"}]
        findings = labs.assess(slip, targets={"LDL": "70 mg/dL"})
        self.assertEqual(findings[0].level, "above_target")
        self.assertEqual(findings[0].target, 70.0)

    def test_a_row_with_no_value_is_pending_doctor_review(self) -> None:
        findings = labs.assess([{"analyte": "Troponin", "value": "", "unit": ""}])
        self.assertEqual(findings[0].level, "cannot_judge")
        self.assertIn(labs.CANNOT_JUDGE_NOTE, findings[0].line)


class ANegationIsNotAReport(unittest.TestCase):
    """S11 wave A round 2, kernel review F3 and F4.

    The first pass matched an abdomen word and a pain word anywhere in the
    joined haystack, and compared stems as bare substrings. The reviewer traced
    five sentences that fired and should not: a patient with a positive test who
    says she has NO abdominal pain was sent the emergency block, which is the
    exact harm item 2 existed to stop, reintroduced by a negation nobody read.
    """

    NOT_PAIN = (
        "no abdominal pain",
        "مفيش وجع في بطني",
        "my abdomen is fine, no pain",
        "my belly is stable",
        "headache and my belly is big",
        "without abdominal pain",
        "لا يوجد وجع في البطن",
        "not abdominal pain, just nausea",
        "مش وجع في بطني، ده غثيان",
    )

    IS_PAIN = (
        "معدتي بتوجعني",
        "my stomach hurts",
        "بطني بتقطعني",
        "batny bt2ata3ny",
        "مصاريني بتوجعني",
        "وجع تحت السرة",
        "ma3deti bt wga3ny",
        "my stomach is cramping badly",
    )

    def test_a_negated_report_does_not_fire(self) -> None:
        for text in self.NOT_PAIN:
            with self.subTest(text=text):
                self.assertFalse(labs.abdominal_pain(text))

    def test_a_negated_report_leaves_a_positive_test_at_urgent_review(self) -> None:
        for text in self.NOT_PAIN:
            with self.subTest(text=text):
                self.assertEqual(
                    labs.judge("Pregnancy test", None, flag="positive", context=text),
                    "urgent_review",
                )

    def test_the_commonest_egyptian_phrasings_do_fire(self) -> None:
        for text in self.IS_PAIN:
            with self.subTest(text=text):
                self.assertTrue(labs.abdominal_pain(text))

    def test_a_stem_matches_at_a_token_start_and_not_inside_a_word(self) -> None:
        """"headache" is not an abdominal ache and "stable" is not a stab."""
        self.assertFalse(labs.abdominal_pain("my belly, and a headache"))
        self.assertFalse(labs.abdominal_pain("my belly is stable"))
        self.assertTrue(labs.abdominal_pain("my belly aches"))

    def test_the_two_words_have_to_be_near_each_other(self) -> None:
        """An abdomen word in one sentence and a pain word six sentences later
        are two facts about two things, not one report."""
        self.assertFalse(labs.abdominal_pain(
            "my belly is fine. my mother has chronic pain in her knee and it aches"
        ))
        self.assertTrue(labs.abdominal_pain("my belly has been in pain"))

    def test_an_arabic_conjunction_prefix_still_matches(self) -> None:
        self.assertTrue(labs.abdominal_pain("وبطني بتوجعني"))


class TheCardSaysWhetherItLooked(unittest.TestCase):
    """S11 wave A round 2, kernel review F2.

    The first pass printed "no abdominal pain reported in the last 48 hours"
    on every unmatched positive test, including the ones where no context was
    handed in and nothing was searched at all. That is the same error class as
    Codex item 3: a check that was not done, reported as done.
    """

    SLIP = [{"analyte": "Pregnancy test", "value": "", "unit": "",
             "ref_range": "", "flag": "Positive"}]

    def test_no_context_at_all_says_the_messages_were_not_checked(self) -> None:
        findings = labs.assess(self.SLIP)
        self.assertEqual(findings[0].level, "urgent_review")
        self.assertIn(labs.PREGNANCY_NOT_CHECKED_NOTE, findings[0].line)
        self.assertNotIn("no abdominal pain reported", findings[0].line)

    def test_a_context_with_no_pain_in_it_says_none_was_found(self) -> None:
        findings = labs.assess(self.SLIP, context="I feel completely fine today")
        self.assertEqual(findings[0].level, "urgent_review")
        self.assertIn(labs.PREGNANCY_NONE_FOUND_NOTE, findings[0].line)

    def test_an_empty_history_is_still_a_history_that_was_read(self) -> None:
        findings = labs.assess(self.SLIP, context=[])
        self.assertIn(labs.PREGNANCY_NONE_FOUND_NOTE, findings[0].line)

    def test_the_two_wordings_are_not_the_same_sentence(self) -> None:
        self.assertNotEqual(labs.PREGNANCY_NOT_CHECKED_NOTE,
                            labs.PREGNANCY_NONE_FOUND_NOTE)
        self.assertIn("not checked", labs.PREGNANCY_NOT_CHECKED_NOTE)
        self.assertIn("48 hours", labs.PREGNANCY_NONE_FOUND_NOTE)

    def test_pain_in_the_context_still_reaches_critical(self) -> None:
        findings = labs.assess(self.SLIP, context="بطني بتوجعني من الصبح")
        self.assertEqual(findings[0].level, "critical")


class WhichPanelAnAnalyteBelongsTo(unittest.TestCase):
    """The matching aid core/photos.py uses to pick between two open tests.

    It never touches whether a value is critical; these tests only assert that
    a slip's analytes point at the doctor's own words for the panel.
    """

    def test_an_analyte_carries_its_own_name_and_its_panel(self) -> None:
        words = labs.panel_words("Potassium (K+)")
        self.assertIn("potassium", words)
        self.assertIn("electrolytes", words)
        self.assertIn("kidney", words)
        self.assertNotIn("lipid", words)

    def test_an_analyte_with_no_table_row_still_has_a_panel(self) -> None:
        """Triglycerides are not a critical value, but they are a lipid panel."""
        self.assertIsNone(labs.rule_for("Triglycerides"))
        self.assertIn("lipid", labs.panel_words("Triglycerides"))
        self.assertIn("kidney", labs.panel_words("Urea"))

    def test_electrolytes_overlap_a_kidney_loop_and_not_a_lipid_one(self) -> None:
        rows = ["Urea", "Creatinine", "Sodium (Na+)", "Potassium (K+)"]
        self.assertGreater(labs.panel_overlap("Kidney function tests", rows), 0)
        self.assertEqual(labs.panel_overlap("Lipid panel", rows), 0)

    def test_lipids_overlap_a_lipid_loop_and_not_a_kidney_one(self) -> None:
        rows = ["Total Cholesterol", "Triglycerides", "HDL Cholesterol",
                "LDL Cholesterol"]
        self.assertGreater(labs.panel_overlap("Lipid panel", rows), 0)
        self.assertEqual(labs.panel_overlap("Kidney function tests", rows), 0)

    def test_an_unknown_panel_overlaps_nothing_rather_than_guessing(self) -> None:
        self.assertEqual(labs.panel_overlap("Lipid panel", ["Vitamin D"]), 0)
        self.assertEqual(labs.panel_overlap("Lipid panel", []), 0)


class UnitAwareJudging(unittest.TestCase):
    """S5 item D11. The table is written in one unit, so the value is converted.

    The red team sent a haemoglobin of 60 g/L, which is 6.0 g/dL and a
    transfusion. Read as a bare 60 it came back in range.
    """

    def test_haemoglobin_in_g_per_litre_is_converted(self) -> None:
        self.assertEqual(labs.judge("Hb", 60.0, unit="g/L"), "critical")
        self.assertEqual(labs.judge("Hb", 60.0, unit="g/dL"), "normal")

    def test_the_common_alternates_all_convert(self) -> None:
        for analyte, value, unit, level in (
            ("Glucose", 2.5, "mmol/L", "critical"),   # 45 mg/dL
            ("Glucose", 6.0, "mmol/L", "normal"),     # 108 mg/dL
            ("Creatinine", 500.0, "umol/L", "critical"),   # 5.7 mg/dL
            ("Creatinine", 88.0, "µmol/L", "normal"),      # 1.0 mg/dL
            ("Calcium", 1.4, "mmol/L", "critical"),   # 5.6 mg/dL
            ("K", 6.4, "mEq/L", "critical"),          # mEq/L == mmol/L
        ):
            with self.subTest(analyte=analyte, unit=unit):
                self.assertEqual(labs.judge(analyte, value, unit=unit), level)

    def test_a_unit_nobody_can_convert_is_urgent_not_normal(self) -> None:
        self.assertEqual(labs.judge("Hb", 60.0, unit="parsecs"), "urgent_review")

    def test_a_slip_with_no_unit_column_reads_as_the_table_unit(self) -> None:
        self.assertEqual(labs.judge("K", 6.4, unit=""), "critical")


class ParsingTheHardValues(unittest.TestCase):
    """S5 item D12. What a printer, a phone camera and a lab can do to a number."""

    def test_scientific_notation(self) -> None:
        self.assertEqual(labs.parse_value("6.0E1"), 60.0)
        self.assertEqual(labs.parse_value("1.2e-1"), 0.12)

    def test_thousands_separators_and_arabic_indic_digits(self) -> None:
        self.assertEqual(labs.parse_value("12,500"), 12500.0)
        self.assertEqual(labs.parse_value("٦٫٤"), 6.4)

    def test_bounded_values_keep_their_number(self) -> None:
        self.assertEqual(labs.parse_value("<0.01"), 0.01)
        self.assertEqual(labs.parse_value("> 12"), 12.0)

    def test_a_value_that_cannot_be_read_but_is_flagged_is_urgent(self) -> None:
        findings = labs.assess([{"analyte": "Potassium", "value": "see comment",
                                 "unit": "mmol/L", "flag": "HH"}])
        self.assertEqual(findings[0].level, "urgent_review")
        self.assertIn(labs.URGENT_REVIEW_NOTE, findings[0].line)

    def test_a_value_that_cannot_be_read_and_is_not_flagged_is_unchanged(self) -> None:
        findings = labs.assess([{"analyte": "Potassium", "value": "see comment",
                                 "unit": "mmol/L", "flag": ""}])
        self.assertEqual(findings[0].level, "cannot_judge")


class AliasesAndUnknownAnalytes(unittest.TestCase):
    """S5 item D13. Every name a lab prints, and what happens to the rest."""

    def test_the_abbreviations_a_lab_actually_prints(self) -> None:
        for written, expected in (
            ("Potass.", "K"), ("K+", "K"), ("Na", "Na"), ("HGB", "Hb"),
            ("Leukocytes", "WBC"), ("TLC", "WBC"), ("Creat.", "Creatinine"),
            ("Cr", "Creatinine"), ("FBS", "Glucose"), ("RBS", "Glucose"),
            ("blood sugar", "Glucose"), ("INR", "INR"), ("PLT", "Platelets"),
            ("Trop", "Troponin"), ("hs-TnT", "Troponin"), ("hs-TnI", "Troponin"),
            ("D-Dimer", "D-dimer"),
        ):
            with self.subTest(written=written):
                rule = labs.rule_for(written)
                self.assertIsNotNone(rule, written)
                self.assertEqual(rule.analyte, expected, written)

    def test_an_unknown_analyte_the_lab_calls_extreme_goes_to_urgent_review(self) -> None:
        self.assertEqual(labs.judge("Ferritin", 2000.0, flag="HH"), "urgent_review")
        self.assertEqual(labs.judge("Ferritin", 2000.0, flag="critical"), "urgent_review")

    def test_an_unknown_analyte_with_an_ordinary_flag_is_unchanged(self) -> None:
        self.assertEqual(labs.judge("HDL", 40.0, flag="H"), "cannot_judge")
        self.assertEqual(labs.judge("HDL", 40.0), "cannot_judge")

    def test_urgent_rows_are_collected_for_the_card(self) -> None:
        findings = labs.assess([
            {"analyte": "Haemoglobin", "value": "9.0", "unit": "pints"},
            {"analyte": "Potassium", "value": "4.2", "unit": "mmol/L"},
        ])
        self.assertEqual([f.analyte for f in labs.urgents(findings)], ["Haemoglobin"])
        self.assertEqual(labs.criticals(findings), [])


class TheEctopicRuleNeedsTwoFacts(unittest.TestCase):
    """Public Codex adversarial regression, S11 wave A item 2:

    "HIGH Any positive pregnancy test becomes critical without abdominal pain
    (SAFETY.md:96 requires both; labs.py:78 LabRule needs_slip_cutoff,
    labs.py:562)."

    docs/SAFETY.md writes the row as "Pregnancy test positive with abdominal
    pain reported in the same conversation (ectopic rule; needs both)". A
    positive test on its own is a result the doctor has to read tonight, not an
    emergency instruction to a patient who may simply be pregnant.
    """

    def test_a_positive_test_on_its_own_is_urgent_review_not_critical(self) -> None:
        self.assertEqual(labs.judge("Pregnancy test", None, flag="POSITIVE"),
                         "urgent_review")
        self.assertEqual(labs.judge("bhcg", None, flag="positive"), "urgent_review")

    def test_a_positive_test_with_abdominal_pain_in_the_context_is_critical(self) -> None:
        for context in (
            "I have bad abdominal pain since last night",
            "بطني بتوجعني جامد من امبارح",
            "3andi maghs fe batni",
            "my belly hurts",
        ):
            with self.subTest(context=context):
                self.assertEqual(
                    labs.judge("Pregnancy test", None, flag="POSITIVE",
                               context=context),
                    "critical",
                )

    def test_context_without_abdominal_pain_does_not_make_it_critical(self) -> None:
        for context in ("my head hurts", "عندي صداع", "I feel fine",
                        "my abdomen is fine", ""):
            with self.subTest(context=context):
                self.assertEqual(
                    labs.judge("Pregnancy test", None, flag="POSITIVE",
                               context=context),
                    "urgent_review",
                )

    def test_a_negative_test_is_not_escalated_either_way(self) -> None:
        self.assertEqual(labs.judge("Pregnancy test", None, flag="negative"),
                         "cannot_judge")
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="negative",
                       context="my belly hurts"),
            "cannot_judge",
        )

    def test_the_context_may_be_several_messages_and_a_caption(self) -> None:
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="H",
                       context=["I took the test", "and my lower belly is cramping"]),
            "critical",
        )

    def test_the_card_line_says_which_half_of_the_rule_is_missing(self) -> None:
        findings = labs.assess(
            [{"analyte": "Pregnancy test", "value": "", "unit": "",
              "ref_range": "", "flag": "Positive"}]
        )
        self.assertEqual(findings[0].level, "urgent_review")
        self.assertIn("abdominal pain: not checked", findings[0].line)
        self.assertEqual(labs.criticals(findings), [])
        self.assertEqual(len(labs.urgents(findings)), 1)

    def test_assess_carries_the_context_through(self) -> None:
        findings = labs.assess(
            [{"analyte": "Pregnancy test", "value": "", "unit": "",
              "ref_range": "", "flag": "Positive"}],
            context="وجع في بطني من الصبح",
        )
        self.assertEqual(findings[0].level, "critical")
        self.assertEqual(len(labs.criticals(findings)), 1)

    def test_the_abdominal_pain_reader_normalises_like_the_sentinel(self) -> None:
        """Diacritics, letter variants and Franco spellings, one alphabet."""
        for text in ("وَجَع في بَطني", "batni bt wga3ny", "lower abdominal cramps",
                     "pelvic pain", "مغص شديد"):
            with self.subTest(text=text):
                self.assertTrue(labs.abdominal_pain(text))
        for text in ("chest pain", "وجع في صدري", "headache", "", None):
            with self.subTest(text=text):
                self.assertFalse(labs.abdominal_pain(text))


# --------------------------------------------------------------------------- #
# S11 wave A item 15: the immutable snapshot and the boundary of every rule
# --------------------------------------------------------------------------- #
# Public Codex adversarial-review finding reproduced by the regression below:
#
#   "MEDIUM Safety-table regression coverage not mutation-complete
#   (test_sentinel.py:16 iterates the current table; test_labs.py:53 pins
#   selected values only). Fix: immutable snapshots, boundary tests per rule."
#
# Everything above pins the values somebody thought to write down: potassium,
# haemoglobin, creatinine. Nothing above notices if the sodium row is deleted or
# if the calcium ceiling moves from 13 to 30. The literal below is a copy of the
# critical-value table frozen as text in this file. It is not read from the
# module and nothing derives it, so the only way to change the table without
# this test failing is to type the change here as well.
#
# If this comparison fails, that is the rail doing its job. A doctor approved
# this table before it was written down (docs/SAFETY.md, "The critical-lab
# table"); an agent does not quietly edit it. A deliberate change goes into
# three places at once: core/labs.py, this literal, and docs/SAFETY.md.
#
# Each row is (analyte, unit, low, high, needs_slip_cutoff, baseline_multiple,
# two_factor), which is every field of labs.LabRule that decides anything. The
# note is prose and is deliberately not frozen.

FROZEN_CRITICAL_LABS = (
    ('K', 'mmol/L', 2.5, 6.0, False, None, False),
    ('Na', 'mmol/L', 120, 160, False, None, False),
    ('Glucose', 'mg/dL', 50, 500, False, None, False),
    ('Creatinine', 'mg/dL', None, 4, False, 2, False),
    ('Hb', 'g/dL', 7, None, False, None, False),
    ('Troponin', '', None, None, True, None, False),
    ('INR', '', None, 5, False, None, False),
    ('Platelets', 'x10^3/uL', 50, None, False, None, False),
    ('D-dimer', '', None, None, True, None, False),
    ('Calcium', 'mg/dL', 6.5, 13, False, None, False),
    ('WBC', 'x10^3/uL', 1.0, 50, False, None, False),
    ('Bilirubin (neonate)', 'mg/dL', None, None, True, None, False),
    ('pH', '', 7.2, 7.6, False, None, False),
    ('HCO3', 'mmol/L', 10, None, False, None, False),
    ('Culture (blood/CSF)', '', None, None, True, None, False),
    ('Pregnancy test', '', None, None, True, None, True),
    ('LDL', 'mg/dL', None, None, False, None, False),
)

FROZEN_UNIT_CONVERSIONS = {
    ('Hb', 'g/l'): 0.1,
    ('Hb', 'gm/l'): 0.1,
    ('Hb', 'mmol/l'): 1.611,
    ('Glucose', 'mmol/l'): 18.0182,
    ('Creatinine', 'umol/l'): 0.011312,
    ('Creatinine', 'µmol/l'): 0.011312,
    ('Calcium', 'mmol/l'): 4.008,
    ('K', 'meq/l'): 1.0,
    ('Na', 'meq/l'): 1.0,
    ('HCO3', 'meq/l'): 1.0,
    ('WBC', 'x10^9/l'): 1.0,
    ('WBC', '10^9/l'): 1.0,
    ('WBC', '/ul'): 0.001,
    ('WBC', 'cells/ul'): 0.001,
    ('Platelets', 'x10^9/l'): 1.0,
    ('Platelets', '10^9/l'): 1.0,
    ('Platelets', '/ul'): 0.001,
    ('Platelets', 'x10^3/l'): 1.0,
}


class TheFrozenCriticalTable(unittest.TestCase):
    def test_the_table_is_exactly_the_frozen_copy(self) -> None:
        live = tuple(
            (r.analyte, r.unit, r.low, r.high, r.needs_slip_cutoff,
             r.baseline_multiple, r.two_factor)
            for r in labs.CRITICAL_LABS
        )
        self.assertEqual(live, FROZEN_CRITICAL_LABS)

    def test_not_one_analyte_has_been_removed(self) -> None:
        live = {r.analyte for r in labs.CRITICAL_LABS}
        frozen = {row[0] for row in FROZEN_CRITICAL_LABS}
        self.assertEqual(frozen - live, set(), "rows removed from the table")
        self.assertEqual(live - frozen, set(), "rows added without freezing them")

    def test_the_unit_conversions_are_exactly_the_frozen_copy(self) -> None:
        """A conversion factor is a threshold in disguise: change 0.1 to 1.0 and
        a haemoglobin of 60 g/L stops being a transfusion."""
        self.assertEqual(dict(labs.UNIT_CONVERSIONS), FROZEN_UNIT_CONVERSIONS)

    def test_every_alias_still_reaches_a_row_that_exists(self) -> None:
        for written, analyte in labs.ALIASES.items():
            with self.subTest(alias=written):
                rule = labs.rule_for(written)
                self.assertIsNotNone(rule, written)
                self.assertEqual(rule.analyte, analyte)


class TheBoundaryOfEveryRule(unittest.TestCase):
    """Just below, at, and just above, for every threshold in the table.

    The table is written as strict inequalities: critical BELOW `low` and ABOVE
    `high`, so the boundary value itself is not critical. That is the line a
    doctor reads off docs/SAFETY.md, and it is asserted here for every row
    rather than for the three rows somebody happened to test.
    """

    #  A margin small enough to be the next value a lab could print, and large
    #  enough that no float comparison decides the answer.
    MARGIN = 0.001

    def bounds(self):
        for rule in labs.CRITICAL_LABS:
            if rule.low is not None:
                yield rule, "low", float(rule.low)
            if rule.high is not None:
                yield rule, "high", float(rule.high)

    def test_the_table_still_has_a_threshold_to_test(self) -> None:
        """A guard on the guard: if the rows stop carrying numbers, the loops
        below would pass by iterating nothing."""
        tested = {rule.analyte for rule, _side, _value in self.bounds()}
        self.assertEqual(
            tested,
            {"K", "Na", "Glucose", "Creatinine", "Hb", "INR", "Platelets",
             "Calcium", "WBC", "pH", "HCO3"},
        )

    def test_every_threshold_in_the_tables_own_unit(self) -> None:
        for rule, side, value in self.bounds():
            step = max(self.MARGIN, abs(value) * self.MARGIN)
            below = labs.judge(rule.analyte, value - step, unit=rule.unit)
            at = labs.judge(rule.analyte, value, unit=rule.unit)
            above = labs.judge(rule.analyte, value + step, unit=rule.unit)
            with self.subTest(analyte=rule.analyte, side=side, value=value):
                self.assertEqual(at, "normal", "the boundary itself is not critical")
                if side == "low":
                    self.assertEqual(below, "critical")
                    self.assertEqual(above, "normal")
                else:
                    self.assertEqual(below, "normal")
                    self.assertEqual(above, "critical")

    def test_every_threshold_in_every_unit_the_table_converts(self) -> None:
        """The red team's haemoglobin: 60 g/L is 6.0 g/dL and a transfusion.
        Every alternate unit is walked across its own row's boundary."""
        seen = 0
        for rule, side, value in self.bounds():
            for (analyte, unit), factor in labs.UNIT_CONVERSIONS.items():
                if analyte != rule.analyte:
                    continue
                seen += 1
                printed = value / factor
                step = abs(printed) * self.MARGIN
                below = labs.judge(rule.analyte, printed - step, unit=unit)
                at = labs.judge(rule.analyte, printed, unit=unit)
                above = labs.judge(rule.analyte, printed + step, unit=unit)
                with self.subTest(analyte=rule.analyte, unit=unit, side=side):
                    self.assertEqual(at, "normal")
                    if side == "low":
                        self.assertEqual(below, "critical")
                        self.assertEqual(above, "normal")
                    else:
                        self.assertEqual(below, "normal")
                        self.assertEqual(above, "critical")
        self.assertEqual(seen, 26, "a conversion stopped being exercised")

    def test_the_creatinine_baseline_multiple_has_a_boundary_too(self) -> None:
        """Two times the patient's own, and the rule is >=, not >."""
        self.assertEqual(labs.judge("Creatinine", 1.99, baseline=1.0), "normal")
        self.assertEqual(labs.judge("Creatinine", 2.0, baseline=1.0), "critical")
        self.assertEqual(labs.judge("Creatinine", 2.01, baseline=1.0), "critical")

    def test_ldl_has_no_boundary_because_it_never_escalates(self) -> None:
        for value in (0.0, 100.0, 400.0, 10000.0):
            with self.subTest(value=value):
                self.assertNotEqual(labs.judge("LDL", value), "critical")

    def test_the_cutoff_relative_rows_have_the_slips_boundary(self) -> None:
        """Troponin, D-dimer, neonatal bilirubin and cultures carry no number of
        their own, so the boundary tested is the one the slip printed."""
        for analyte in ("Troponin", "D-dimer", "Bilirubin (neonate)",
                        "Culture (blood/CSF)"):
            with self.subTest(analyte=analyte):
                self.assertEqual(
                    labs.judge(analyte, 0.39, ref_range="< 0.40"), "cannot_judge")
                self.assertEqual(
                    labs.judge(analyte, 0.40, ref_range="< 0.40"), "cannot_judge")
                self.assertEqual(
                    labs.judge(analyte, 0.41, ref_range="< 0.40"), "critical")
                self.assertEqual(labs.judge(analyte, None), "cannot_judge")

    def test_the_pregnancy_row_needs_both_halves_at_its_boundary(self) -> None:
        """The one two-factor row. The first fact alone stops at urgent review;
        the second fact is what carries it to critical."""
        self.assertEqual(labs.judge("Pregnancy test", None, flag=""), "cannot_judge")
        self.assertEqual(labs.judge("Pregnancy test", None, flag="negative"),
                         "cannot_judge")
        self.assertEqual(labs.judge("Pregnancy test", None, flag="positive"),
                         "urgent_review")
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="positive",
                       context="no pain anywhere"),
            "urgent_review")
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="positive",
                       context="my abdomen hurts"),
            "critical")
        # The same two steps read off the slip's own printed cutoff instead of
        # its flag word.
        self.assertEqual(labs.judge("Pregnancy test", 30.0, ref_range="< 25"),
                         "urgent_review")
        self.assertEqual(
            labs.judge("Pregnancy test", 30.0, ref_range="< 25",
                       context="بطني بتوجعني"),
            "critical")


# --------------------------------------------------------------------------- #
# S11 wave A round 2, kernel review F9: the rest of the decision tables
# --------------------------------------------------------------------------- #
# Round 1 froze the critical-value table and the unit conversions, and docs/
# SAFETY.md then said "the tables are frozen, not just exercised". Five tables
# that decide things were not frozen, and two of them this wave introduced:
#
#   ABDOMEN_WORDS, PAIN_WORDS, ABDOMINAL_PAIN_PHRASES, NEGATION_WORDS decide
#   whether a positive pregnancy test sends the emergency block to a patient;
#   HIGH_FLAGS, LOW_FLAGS, URGENT_FLAGS, EXTREME_FLAGS decide whether a lab's
#   own word is obeyed (remove "positive" from HIGH_FLAGS and a positive
#   troponin quietly becomes "cannot judge");
#   ALIASES decides whether a row is found at all.
#
# Same rule as the tables above: typed out here, not read from the module, and a
# deliberate change is a change in three places at once.

FROZEN_ABDOMEN_WORDS = (
    'abdomen',
    'abdominal',
    'belly',
    'tummy',
    'stomach',
    'pelvic',
    'pelvis',
    'بطن',
    'بطني',
    'البطن',
    'بطنها',
    'الحوض',
    'حوضي',
    'جنبي',
    'معده',
    'معدتي',
    'المعده',
    'مصارين',
    'مصاريني',
    'السره',
    'سرتي',
    'batn',
    'batni',
    'beten',
    'batny',
    '7od',
    'ma3da',
    'ma3deti',
    'ma3dety',
    'masareen',
    'masarini',
)

FROZEN_PAIN_WORDS = (
    'pain',
    'ache*',
    'hurt*',
    'cramp*',
    'colic',
    'sore',
    'tender',
    'spasm*',
    'stabbing',
    'stabbed',
    'sharp',
    'burning',
    'وجع*',
    'واجع*',
    'بتوجع*',
    'بيوجع*',
    'الم',
    'الام',
    'مغص',
    'تقلص*',
    'بتقطع*',
    'بيقطع*',
    'قطع*',
    'طلق',
    'حرقان',
    'wag3*',
    'wga3*',
    'waga3*',
    'alam',
    'maghs',
    'mogs',
    'mag9',
    'bt2ata3*',
    'bt2ta3*',
    'bit2ata3*',
    'tal2',
)

FROZEN_ABDOMINAL_PAIN_PHRASES = (
    'abdominal pain',
    'belly pain',
    'stomach pain',
    'stomach ache',
    'stomach cramps',
    'pelvic pain',
    'ectopic',
    'lower abdominal',
    'مغص',
    'حمل خارج الرحم',
    'وجع في بطني',
    'وجع بطن',
    'الم في البطن',
    'تحت السره',
    'maghs',
    '7aml barra el ra7m',
)

FROZEN_NEGATION_WORDS = (
    'no',
    'not',
    'never',
    'without',
    'none',
    'denies',
    'denied',
    'free',
    'مفيش',
    'مافيش',
    'ماعنديش',
    'معنديش',
    'مش',
    'لا',
    'بدون',
    'ولا',
    'mafish',
    'mafeesh',
    'mesh',
    'ma3andish',
    'ma3andesh',
    'bidoun',
    'bedoun',
)

FROZEN_HIGH_FLAGS = (
    'h',
    'hh',
    'high',
    'elevated',
    'positive',
    'pos',
    'reactive',
    'abnormal',
    'critical',
    'panic',
    'detected',
    'مرتفع',
    'عالي',
    'ايجابي',
    'موجب',
)

FROZEN_LOW_FLAGS = (
    'l',
    'll',
    'low',
    'منخفض',
    'قليل',
)

# S16, from S15 defect 2. The tables above are read a second way: a flag that is
# the beginning of one of their words, and this many characters long or longer,
# is that word ("POSITIV" is "positive"). The number decides escalations, so it
# is frozen beside the words it reads. Lowering it to 3 would make "pos" a
# truncation of "positive" rather than the printed flag it already is, and would
# let "cri", "hig" and "det" through with it.
FROZEN_FLAG_PREFIX_MIN = 5

FROZEN_URGENT_FLAGS = (
    'h',
    'hh',
    'l',
    'll',
    'critical',
    'panic',
    'high',
    'low',
    'abnormal',
    'مرتفع',
    'منخفض',
)

FROZEN_EXTREME_FLAGS = (
    'hh',
    'll',
    'critical',
    'panic',
)

FROZEN_ALIASES = {
    'potassium': 'K',
    'k+': 'K',
    'kplus': 'K',
    'potass': 'K',
    'pot': 'K',
    'sodium': 'Na',
    'na+': 'Na',
    'sod': 'Na',
    'glucose fasting': 'Glucose',
    'fasting glucose': 'Glucose',
    'blood glucose': 'Glucose',
    'blood sugar': 'Glucose',
    'fbs': 'Glucose',
    'rbs': 'Glucose',
    'random blood sugar': 'Glucose',
    'sugar': 'Glucose',
    'creat': 'Creatinine',
    'cr': 'Creatinine',
    'creatinin': 'Creatinine',
    'hemoglobin': 'Hb',
    'haemoglobin': 'Hb',
    'hgb': 'Hb',
    'hb%': 'Hb',
    'haemoglobin hb': 'Hb',
    'hemoglobin hgb': 'Hb',
    'troponin i': 'Troponin',
    'troponin t': 'Troponin',
    'hs troponin': 'Troponin',
    'high sensitivity troponin': 'Troponin',
    'trop': 'Troponin',
    'hs tnt': 'Troponin',
    'hs tni': 'Troponin',
    'hstnt': 'Troponin',
    'hstni': 'Troponin',
    'troponin hs': 'Troponin',
    'd dimer': 'D-dimer',
    'ddimer': 'D-dimer',
    'd-dimer quantitative': 'D-dimer',
    'd dimer quantitative': 'D-dimer',
    'plt': 'Platelets',
    'platelet': 'Platelets',
    'platelet count': 'Platelets',
    'ca': 'Calcium',
    'ca2+': 'Calcium',
    'wbc count': 'WBC',
    'white blood cells': 'WBC',
    'white cell count': 'WBC',
    'leukocytes': 'WBC',
    'tlc': 'WBC',
    'ldl c': 'LDL',
    'ldl cholesterol': 'LDL',
    'ldlc': 'LDL',
    'bicarbonate': 'HCO3',
    'hco3-': 'HCO3',
    'co2': 'HCO3',
    'ph': 'pH',
    'bilirubin neonate': 'Bilirubin (neonate)',
    'neonatal bilirubin': 'Bilirubin (neonate)',
    'blood culture': 'Culture (blood/CSF)',
    'csf culture': 'Culture (blood/CSF)',
    'culture': 'Culture (blood/CSF)',
    'pregnancy': 'Pregnancy test',
    'bhcg': 'Pregnancy test',
    'hcg': 'Pregnancy test',
    'beta hcg': 'Pregnancy test',
    'b hcg': 'Pregnancy test',
    'beta hcg qualitative': 'Pregnancy test',
}


class TheFrozenWordTables(unittest.TestCase):
    def test_the_abdominal_pain_tables_are_exactly_the_frozen_copies(self) -> None:
        for name, frozen in (
            ("ABDOMEN_WORDS", FROZEN_ABDOMEN_WORDS),
            ("PAIN_WORDS", FROZEN_PAIN_WORDS),
            ("ABDOMINAL_PAIN_PHRASES", FROZEN_ABDOMINAL_PAIN_PHRASES),
            ("NEGATION_WORDS", FROZEN_NEGATION_WORDS),
        ):
            with self.subTest(table=name):
                self.assertEqual(tuple(getattr(labs, name)), frozen)

    def test_not_one_negation_has_been_dropped(self) -> None:
        """Dropping a negation is the failure that sends the emergency block to
        a patient who said she has no pain, so it is asserted on the set too."""
        live, frozen = set(labs.NEGATION_WORDS), set(FROZEN_NEGATION_WORDS)
        self.assertEqual(frozen - live, set(), "negations removed")
        self.assertEqual(live - frozen, set(), "negations added without freezing")

    def test_the_flag_tables_are_exactly_the_frozen_copies(self) -> None:
        for name, frozen in (
            ("HIGH_FLAGS", FROZEN_HIGH_FLAGS),
            ("LOW_FLAGS", FROZEN_LOW_FLAGS),
            ("URGENT_FLAGS", FROZEN_URGENT_FLAGS),
            ("EXTREME_FLAGS", FROZEN_EXTREME_FLAGS),
        ):
            with self.subTest(table=name):
                self.assertEqual(tuple(getattr(labs, name)), frozen)

    def test_the_alias_table_is_exactly_the_frozen_copy(self) -> None:
        self.assertEqual(dict(labs.ALIASES), FROZEN_ALIASES)

    def test_the_truncation_length_is_the_frozen_one(self) -> None:
        self.assertEqual(labs.FLAG_PREFIX_MIN, FROZEN_FLAG_PREFIX_MIN)


class TheBoundaryOfEveryFlagWord(unittest.TestCase):
    """A flag word is a threshold made of letters, so it gets a boundary too.

    The cutoff-relative rows have no number of their own: the lab's word IS the
    decision. Remove "positive" from HIGH_FLAGS and a positive troponin becomes
    "cannot judge", and every numeric test in this file stays green.
    """

    def test_every_high_flag_makes_a_cutoff_relative_row_critical(self) -> None:
        for flag in labs.HIGH_FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("Troponin", None, flag=flag), "critical")
                self.assertEqual(labs.judge("D-dimer", None, flag=flag), "critical")

    def test_a_word_that_is_not_a_flag_decides_nothing(self) -> None:
        for flag in ("pending", "sample", "see comment", "normal", "negative",
                     "not detected", ""):
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("Troponin", None, flag=flag),
                                 "cannot_judge")

    def test_every_extreme_flag_lifts_an_unknown_analyte_to_urgent(self) -> None:
        for flag in labs.EXTREME_FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("Ferritin", 2000.0, flag=flag),
                                 "urgent_review")

    def test_every_urgent_flag_lifts_an_unreadable_value_to_urgent(self) -> None:
        for flag in labs.URGENT_FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("K", None, flag=flag), "urgent_review")

    def test_a_flag_still_never_overrules_a_number_the_table_owns(self) -> None:
        for flag in labs.HIGH_FLAGS + labs.LOW_FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("K", 4.2, flag=flag), "normal")


class AFlagWithACharacterMissing(unittest.TestCase):
    """S15 defect 2, found live and reproduced three times on one slip.

    The flag column is transcribed by a model. Two of three renders of the same
    beta hCG slip printed "POSITIV", one character short, and the exact list
    dropped them: the card came back as an ordinary yellow lab result reading
    "cannot judge, pending doctor review" and the ectopic escalation never
    happened. labs.FLAG_PREFIX_MIN is the fix and this is its boundary.
    """

    def test_a_truncated_positive_is_still_positive(self) -> None:
        for flag in ("POSITIV", "POSIT", "positiv", "posit"):
            with self.subTest(flag=flag):
                self.assertTrue(labs.flag_is_high(flag))

    def test_four_characters_are_not_enough_to_be_sure(self) -> None:
        for flag in ("POSI", "posi", "elev", "crit", "reac", "dete", "abno"):
            with self.subTest(flag=flag):
                self.assertFalse(labs.flag_is_high(flag))

    def test_pos_is_high_because_the_table_says_so_not_by_truncation(self) -> None:
        """The one exception the brief's own example asks about.

        "pos" is three characters, so the truncation rule above cannot reach
        it. It is high anyway, because a lab that prints "POS" in its flag
        column has printed a flag, and "pos" has been a frozen word in
        HIGH_FLAGS since S5. Removing it to make this assertion False would
        turn a printed "POS" on a troponin or a pregnancy test back into
        "cannot judge", which is the very failure this file is fixing.
        """
        self.assertIn("pos", labs.HIGH_FLAGS)
        self.assertTrue(labs.flag_is_high("POS"))
        self.assertFalse(labs.flag_is_high("PO"))

    def test_a_truncated_low_is_still_low(self) -> None:
        self.assertTrue(labs.flag_is_low("منخفض"))
        self.assertFalse(labs.flag_is_low("lo"))
        self.assertFalse(labs.flag_is_low("منخف"))

    def test_a_word_that_merely_shares_letters_is_still_not_a_flag(self) -> None:
        for flag in ("normal", "negative", "not detected", "pending", "sample",
                     "see comment", "hemolysed", "posterior", "possible"):
            with self.subTest(flag=flag):
                self.assertFalse(labs.flag_is_high(flag))
                self.assertFalse(labs.flag_is_low(flag))

    def test_the_truncated_flag_carries_the_escalation_it_lost(self) -> None:
        """The live case, end to end: the row the lab owns the cutoff for."""
        self.assertEqual(labs.judge("Troponin", None, flag="POSITIV"), "critical")
        self.assertEqual(labs.judge("Pregnancy test", None, flag="POSITIV"),
                         "urgent_review")

    def test_the_pregnancy_rule_still_needs_both_factors(self) -> None:
        """The truncation restores the first half of the rule and nothing else.

        A positive test alone is a doctor card. It becomes the emergency only
        when the patient's own words in the same conversation report abdominal
        pain, and a stated absence of pain still holds it back.
        """
        self.assertEqual(labs.judge("Pregnancy test", None, flag="POSITIV"),
                         "urgent_review")
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="POSITIV",
                       context="بطني بتوجعني"), "critical")
        self.assertEqual(
            labs.judge("Pregnancy test", None, flag="POSITIV",
                       context="مفيش أي وجع في بطني"), "urgent_review")
        self.assertEqual(labs.judge("Pregnancy test", None, flag="POSI",
                                    context="بطني بتوجعني"), "cannot_judge")

    def test_a_truncation_still_never_overrules_a_number_the_table_owns(self) -> None:
        for flag in ("POSITIV", "منخفض", "abnorm"):
            with self.subTest(flag=flag):
                self.assertEqual(labs.judge("K", 4.2, flag=flag), "normal")


if __name__ == "__main__":
    unittest.main()
