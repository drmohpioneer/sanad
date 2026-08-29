"""The Coordinator's whole vocabulary, and the fact that it is a vocabulary.

An agent with tools that could also write a sentence would be an agent that
could say anything. These are the eleven sentences it can say, in two languages
and three grammatical genders, and the only variable parts are a date, a name
and an analyte. The test that matters most here is the last class: no template
carries a field that is not on the allowed list, so no template is a place a
dose could appear.

Five of the eleven are the Coordinator's own tools (S6). Three were added by the
administrative tier (S6++ item G, core/intents.py): the doctor was told and no
substitute will be suggested, the plan sent again, and where to send a photo.
Three more came with rev 17, and they sit together at the bottom of
core/templates.py under a marked block: the line a patient gets when his own
message was escalated (item 6), and the two bubbles that open the chat before
he has typed anything (item 9).
"""

from __future__ import annotations

import unittest

from core import templates


class TheFiveSentences(unittest.TestCase):
    def test_the_spec_asked_for_these_and_they_are_here(self) -> None:
        self.assertEqual(set(templates.TEMPLATES), {
            "check_again", "cost_told", "send_when_ready", "missing_part",
            "followup_reason",
            # S6++ item G, the administrative tier.
            "told_doctor", "plan_again", "send_it_here",
            # rev 17, items 6, 9 and 11.
            "told_doctor_will_answer", "welcome", "welcome_next",
            "doctor_says"})

    def test_the_rev_17_block_is_one_block_a_reviewer_can_read(self) -> None:
        """Mohamed reads the Arabic in one place, so it is written in one place."""
        from pathlib import Path
        source = (Path(templates.__file__).read_text(encoding="utf-8")
                  .split("# REV 17:", 1)[1])
        for name in ("TOLD_DOCTOR_WILL_ANSWER", "WELCOME", "WELCOME_NEXT",
                     "DOCTOR_SAYS"):
            with self.subTest(name=name):
                self.assertIn(f"{name} = {{", source)

    def test_every_one_exists_in_both_languages_and_three_genders(self) -> None:
        for key, table in templates.TEMPLATES.items():
            for speak in ("ar", "en"):
                for who in ("m", "f", "u"):
                    with self.subTest(key=key, speak=speak, who=who):
                        self.assertTrue(table[speak][who].strip())

    def test_the_arabic_is_written_three_ways_where_the_verb_is_gendered(self) -> None:
        """The bug Mohamed's first phone test found: a woman addressed as a man."""
        for key in ("check_again", "send_when_ready", "missing_part",
                    "followup_reason", "send_it_here"):
            with self.subTest(key=key):
                forms = templates.TEMPLATES[key]["ar"]
                self.assertNotEqual(forms["m"], forms["f"])
                self.assertNotEqual(forms["m"], forms["u"])


class TheVariables(unittest.TestCase):
    def test_no_template_carries_a_field_outside_the_allowed_list(self) -> None:
        for key, table in templates.TEMPLATES.items():
            for speak, forms in table.items():
                for who, text in forms.items():
                    with self.subTest(key=key, speak=speak, who=who):
                        self.assertTrue(
                            templates.fields_of(text) <= templates.ALLOWED_FIELDS,
                            f"{key}/{speak}/{who} carries "
                            f"{templates.fields_of(text)}",
                        )

    def test_the_allowed_list_is_a_date_a_name_and_an_analyte(self) -> None:
        self.assertEqual(templates.ALLOWED_FIELDS,
                         {"patient", "doctor", "date", "analyte"})

    def test_no_template_contains_a_digit(self) -> None:
        """A dose is a number, so the sentences carry none of their own."""
        for key, table in templates.TEMPLATES.items():
            for speak, forms in table.items():
                for who, text in forms.items():
                    with self.subTest(key=key, speak=speak, who=who):
                        self.assertFalse(any(ch.isdigit() for ch in text))

    def test_no_template_uses_a_dash_that_reads_as_a_machine_wrote_it(self) -> None:
        for key, table in templates.TEMPLATES.items():
            for forms in table.values():
                for text in forms.values():
                    with self.subTest(key=key):
                        self.assertNotIn("—", text)
                        self.assertNotIn("–", text)


class Rendering(unittest.TestCase):
    def test_a_date_and_a_name_are_the_only_things_that_move(self) -> None:
        line = templates.render("check_again", "en", "m", patient="Ahmed",
                                date="2026-09-01")
        self.assertIn("Ahmed", line)
        self.assertIn("2026-09-01", line)

    def test_arabic_answers_a_woman_in_the_feminine(self) -> None:
        woman = templates.render("send_when_ready", "ar", "f")
        man = templates.render("send_when_ready", "ar", "m")
        unknown = templates.render("send_when_ready", "ar", "u")
        self.assertNotEqual(woman, man)
        self.assertNotEqual(unknown, man)

    def test_a_missing_field_raises_rather_than_sending_half_a_sentence(self) -> None:
        with self.assertRaises(ValueError):
            templates.render("check_again", "ar", "m", patient="Ahmed")

    def test_an_unknown_template_is_not_quietly_skipped(self) -> None:
        with self.assertRaises(KeyError):
            templates.render("say_anything", "en", "m")

    def test_an_unknown_language_falls_back_to_english_not_to_nothing(self) -> None:
        line = templates.render("send_when_ready", "fr", "u")
        self.assertEqual(line, templates.TEMPLATES["send_when_ready"]["en"]["u"])

    def test_the_cost_line_says_the_doctor_was_told_and_nothing_else(self) -> None:
        """Cost is escalate-only: no advice, no cheaper lab, no opinion."""
        for speak in ("ar", "en"):
            with self.subTest(speak=speak):
                line = templates.render("cost_told", speak, "u", doctor="Dr Mohamed")
                self.assertIn("Dr Mohamed", line)


if __name__ == "__main__":
    unittest.main()
