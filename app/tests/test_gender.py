"""Nobody is addressed in the wrong gender, and nobody is guessed at.

Mohamed's first real phone test found a female patient being written to as a
man. These run in the container build (see the Dockerfile), so a template that
loses its feminine form fails the deploy instead of the demo.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from core import gender, sentinel
from core.models import Doctor, Loop, Patient

try:  # core.chaser reaches Firestore at import; the image has it, a laptop may not
    from core import chaser
except ImportError as exc:  # pragma: no cover - the image build always has it
    raise unittest.SkipTest(f"cloud SDK not installed: {exc}") from exc

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def patient(name: str, sex: str | None) -> Patient:
    return Patient(id="p", doctor_id="d", name=name, sex=sex, created_at=NOW)


DOCTOR = Doctor(id="d", name="Dr Mohamed", web_token="t", created_at=NOW)
LOOP = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
            title="Lipid panel", details={"test_name": "lipid panel"},
            created_at=NOW, updated_at=NOW)
MONITOR = Loop(id="l2", patient_id="p", doctor_id="d", type="MONITOR",
               title="Blood pressure", details={"metric": "BP"},
               created_at=NOW, updated_at=NOW)


class ReadingTheField(unittest.TestCase):
    def test_both_languages_and_both_spellings(self) -> None:
        for value in ("male", "Male", "M", "man", "ذكر", "راجل"):
            with self.subTest(value=value):
                self.assertEqual(gender.of(value), "m")
        for value in ("female", "F", "woman", "أنثى", "انثى", "سيدة", "بنت"):
            with self.subTest(value=value):
                self.assertEqual(gender.of(value), "f")

    def test_a_phrase_still_decides(self) -> None:
        self.assertEqual(gender.of("58 year old female"), "f")
        self.assertEqual(gender.of("a 61 year old man"), "m")

    def test_nothing_and_nonsense_are_unknown_not_male(self) -> None:
        for value in (None, "", "   ", "unspecified", "x"):
            with self.subTest(value=value):
                self.assertEqual(gender.of(value), "u")

    def test_a_patient_with_no_sex_field_is_unknown(self) -> None:
        self.assertEqual(gender.of_patient(patient("Hend Ismail", None)), "u")


class EnglishWords(unittest.TestCase):
    def test_the_three_forms(self) -> None:
        self.assertEqual(gender.possessive("m"), "his")
        self.assertEqual(gender.possessive("f"), "her")
        self.assertEqual(gender.possessive("u"), "their")
        self.assertEqual(gender.object_pronoun("f"), "her")
        self.assertEqual(gender.object_pronoun("u"), "them")


class ArabicNudges(unittest.TestCase):
    """The Arabic verb has to move with the patient, in every rung and kind."""

    def text(self, sex: str | None, attempt: int, kind: str = "nudge") -> str:
        return chaser.nudge_text(
            patient("Hend Ismail", sex), DOCTOR,
            MONITOR if kind == "monitor" else LOOP, attempt, "ar", kind,
        )

    def test_a_woman_is_never_addressed_as_a_man(self) -> None:
        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                self.assertNotIn("فاكر إن", self.text("female", attempt))
                self.assertNotIn("ابعتلي", self.text("female", attempt))
        self.assertIn("فاكرة", self.text("female", 1))
        self.assertIn("ابعتيلي", self.text("female", 2))

    def test_a_man_keeps_the_masculine_forms(self) -> None:
        self.assertIn("فاكر إن", self.text("male", 1))
        self.assertIn("ابعتلي", self.text("male", 2))

    def test_unknown_sex_commits_to_neither(self) -> None:
        for attempt in (1, 2, 3):
            line = self.text(None, attempt)
            with self.subTest(attempt=attempt):
                for gendered in ("فاكر", "فاكرة", "ابعتلي", "ابعتيلي",
                                 "تعمله", "تعمليه"):
                    self.assertNotIn(gendered, line)

    def test_the_monitor_reminder_moves_too(self) -> None:
        self.assertIn("تقيس", self.text("male", 1, "monitor"))
        self.assertIn("تقيسي", self.text("female", 1, "monitor"))
        self.assertNotIn("تقيسي", self.text("male", 1, "monitor"))
        self.assertNotIn("تقيس ", self.text(None, 1, "monitor"))

    def test_english_needs_no_variants(self) -> None:
        """English's second person carries no gender: one table, three sexes."""
        lines = {
            chaser.nudge_text(patient("Hend Ismail", sex), DOCTOR, LOOP, 1, "en",
                              "nudge")
            for sex in ("male", "female", None)
        }
        self.assertEqual(len(lines), 1)


class ArabicEmergency(unittest.TestCase):
    def test_the_imperative_moves_with_the_patient(self) -> None:
        self.assertIn("روح أقرب", sentinel.emergency_text("ar", "m"))
        self.assertIn("روحي أقرب", sentinel.emergency_text("ar", "f"))
        self.assertNotIn("روح أقرب", sentinel.emergency_text("ar", "u"))
        self.assertNotIn("روحي", sentinel.emergency_text("ar", "u"))

    def test_every_form_still_says_the_three_things(self) -> None:
        for who in ("m", "f", "u"):
            text = sentinel.emergency_text("ar", who)
            with self.subTest(who=who):
                self.assertIn("123", text)
                self.assertIn("طوارئ", text)
                self.assertNotIn("متقلقش", text)

    def test_english_is_one_string(self) -> None:
        self.assertEqual(
            sentinel.emergency_text("en", "f"), sentinel.emergency_text("en", "m")
        )


class DoctorFacingWords(unittest.TestCase):
    def test_the_unreachable_card_names_the_right_person(self) -> None:
        card = chaser.unreachable_card(patient("Hend Ismail", "female"), LOOP, 3)
        self.assertIn("stops messaging her.", " ".join(card["lines"]))
        card = chaser.unreachable_card(patient("Ahmed Ali", "male"), LOOP, 3)
        self.assertIn("stops messaging him.", " ".join(card["lines"]))
        card = chaser.unreachable_card(patient("Sam", None), LOOP, 0)
        self.assertIn("stops messaging them.", " ".join(card["lines"]))


if __name__ == "__main__":
    unittest.main()
