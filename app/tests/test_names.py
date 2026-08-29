"""Which patient the doctor meant, and what happens when that is not obvious.

The board these tests use is the one that produced the defect: "Ismail Roshdy"
and "Hend Ismail" were both on it, and "/force_due Ismail" chased Hend without
saying there was another candidate. That case is the first test below.
"""

from __future__ import annotations

import unittest

from core import names

BOARD = ["Ismail Roshdy", "Ahmed Ali", "Hend Ismail", "Zeinab Farouk"]


class Resolving(unittest.TestCase):
    def test_a_fragment_two_patients_share_is_ambiguous(self) -> None:
        """S4 results: this used to silently return the first of the two."""
        match = names.resolve(BOARD, "Ismail")
        self.assertTrue(match.ambiguous)
        self.assertIsNone(match.one)
        self.assertEqual(match.names, ("Ismail Roshdy", "Hend Ismail"))

    def test_the_warning_names_every_candidate(self) -> None:
        warning = names.resolve(BOARD, "Ismail").warning()
        self.assertIn("Ismail Roshdy", warning)
        self.assertIn("Hend Ismail", warning)
        self.assertIn("2", warning)

    def test_a_fragment_only_one_patient_has_resolves(self) -> None:
        self.assertEqual(names.resolve(BOARD, "Roshdy").one, "Ismail Roshdy")
        self.assertEqual(names.resolve(BOARD, "Hend").one, "Hend Ismail")

    def test_matching_ignores_case(self) -> None:
        self.assertEqual(names.resolve(BOARD, "ZEINAB").one, "Zeinab Farouk")
        self.assertEqual(names.resolve(BOARD, "ahmed ali").one, "Ahmed Ali")

    def test_nobody_matches(self) -> None:
        match = names.resolve(BOARD, "Mahmoud")
        self.assertFalse(match.ambiguous)
        self.assertIsNone(match.one)
        self.assertIn("Mahmoud", match.nobody())

    def test_an_empty_fragment_matches_nobody_rather_than_everybody(self) -> None:
        for fragment in ("", "   ", None):
            with self.subTest(fragment=fragment):
                match = names.resolve(BOARD, fragment or "")
                self.assertEqual(match.names, ())
                self.assertIsNone(match.one)

    def test_a_whole_name_wins_over_being_part_of_another(self) -> None:
        """A doctor who typed the full name has been as specific as he can be."""
        board = ["Ali", "Ali Hassan", "Mona Ali"]
        self.assertEqual(names.resolve(board, "Ali").one, "Ali")
        self.assertEqual(names.resolve(board, "ali").one, "Ali")
        self.assertEqual(names.resolve(board, "Hassan").one, "Ali Hassan")

    def test_candidates_come_back_in_board_order(self) -> None:
        """Board order is oldest first, which is the order the doctor knows."""
        self.assertEqual(
            names.resolve(BOARD, "a").names,
            ("Ismail Roshdy", "Ahmed Ali", "Hend Ismail", "Zeinab Farouk"),
        )


# --------------------------------------------------------------------------- #
# rev 17, item 11: what an Arabic sentence is allowed to call him
# --------------------------------------------------------------------------- #
class TheNameInsideAnArabicSentence(unittest.TestCase):
    """A Latin name inside an Arabic sentence is the tell of a machine.

    The doctor dictates in English on camera, so the record holds "Ahmed Ali"
    and every Arabic line greeted him as "يا Ahmed". The table is strict and
    there is no fuzzy matching: a wrong transliteration is worse than a Latin
    name, so an unknown name is dropped rather than guessed at.
    """

    def test_a_known_name_is_written_in_arabic(self) -> None:
        for latin, arabic in (("Ahmed Ali", "أحمد"), ("Mohamed", "محمد"),
                              ("Hend Ismail", "هند"), ("fatma", "فاطمة"),
                              ("Youssef Ibrahim", "يوسف")):
            with self.subTest(latin=latin):
                self.assertEqual(names.in_arabic(latin), arabic)

    def test_a_name_already_in_arabic_is_left_exactly_as_dictated(self) -> None:
        self.assertEqual(names.in_arabic("هند إسماعيل"), "هند")
        self.assertEqual(names.in_arabic("عبد الرحمن سيد"), "عبد")

    def test_an_unknown_name_is_dropped_and_never_transliterated(self) -> None:
        for unknown in ("Zbigniew Kowalski", "Xyz", ""):
            with self.subTest(unknown=unknown):
                self.assertEqual(names.in_arabic(unknown), "")
                self.assertEqual(names.vocative(unknown, "ar"), "")

    def test_nothing_here_matches_fuzzily(self) -> None:
        """"Ahmedd" is not Ahmed. A near miss is a miss."""
        for near in ("Ahmedd", "Ahme", "Mohameed", "Al-Ahmed"):
            with self.subTest(near=near):
                self.assertEqual(names.in_arabic(near), "")

    def test_the_vocative_carries_its_own_particle_or_nothing(self) -> None:
        self.assertEqual(names.vocative("Ahmed Ali", "ar"), "يا أحمد")
        self.assertEqual(names.vocative("Zbigniew", "ar"), "")

    def test_english_keeps_the_name_the_doctor_dictated(self) -> None:
        self.assertEqual(names.vocative("Ahmed Ali", "en"), "Ahmed")
        self.assertEqual(names.vocative("Zbigniew Kowalski", "en"), "Zbigniew")

    def test_the_table_is_a_table_and_not_a_rule(self) -> None:
        self.assertGreater(len(set(names.ARABIC_FIRST_NAMES.values())), 60)
        for latin, arabic in names.ARABIC_FIRST_NAMES.items():
            with self.subTest(latin=latin):
                self.assertEqual(latin, latin.lower())
                self.assertTrue(names.is_arabic(arabic))
                self.assertFalse(names.is_arabic(latin))

    def test_the_identity_check_is_untouched_by_any_of_it(self) -> None:
        """Display is display. A slip is still matched against the record."""
        ok, why = names.same_person("Ahmed Ali", "Ahmed Ali")
        self.assertTrue(ok)
        ok, why = names.same_person("أحمد علي", "Ahmed Ali")
        self.assertFalse(ok, "transliteration must never reach same_person")


if __name__ == "__main__":
    unittest.main()
