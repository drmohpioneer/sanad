"""The twenty background patients, and the promise the runbook makes about them.

S6++ item J. Two things are tested here that are easy to get wrong and expensive
to get wrong on camera:

  the board       twenty invented patients, one to three obligations each, in
                  every state, so the end-of-day summary counts something real;
  the runbook     the numbers printed in docs/RUNBOOK.md are the numbers this
                  fixture actually produces. They are computed, then compared
                  with the document, so nobody can quietly reword either.

Pure: the fixture and the counting need no database. `seed()` is the only
function that writes and it is not called here.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import background, monitoring, summary

DOCTOR = "testdoctor00000000"
NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
RUNBOOK = (Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md")


class TwentyPatients(unittest.TestCase):
    def setUp(self) -> None:
        self.patients, self.loops, self.events, self.relays = background.records(
            DOCTOR, NOW)

    def test_there_are_twenty_of_them(self) -> None:
        self.assertEqual(len(self.patients), 20)
        self.assertEqual(len(background.PEOPLE), 20)

    def test_each_carries_one_to_three_obligations(self) -> None:
        for person in background.PEOPLE:
            with self.subTest(person=person.name):
                self.assertGreaterEqual(len(person.contracts), 1)
                self.assertLessEqual(len(person.contracts), 3)

    def test_both_genders_and_both_languages_are_on_the_board(self) -> None:
        self.assertEqual({p.sex for p in background.PEOPLE}, {"male", "female"})
        self.assertEqual({p.speak for p in background.PEOPLE}, {"ar", "en"})

    def test_the_specialties_are_more_than_one_clinic(self) -> None:
        specialties = {p.specialty for p in background.PEOPLE}
        for wanted in ("cardiology", "endocrinology", "nephrology",
                       "obstetrics", "paediatrics", "general medicine"):
            with self.subTest(specialty=wanted):
                self.assertIn(wanted, specialties)
        self.assertGreaterEqual(len(specialties), 8)

    def test_every_state_the_summary_can_count_is_represented(self) -> None:
        buckets = {summary.classify(loop, summary.critical_loops(self.events))
                   for loop in self.loops}
        self.assertEqual(buckets, {"critical", "unreachable", "needs_help",
                                   "completed_with_evidence",
                                   "closed_without_evidence", "progressing"})

    def test_nobody_carries_a_real_phone_number(self) -> None:
        for patient in self.patients:
            with self.subTest(patient=patient.name):
                self.assertTrue(patient.phone.startswith("0100 000 00"))

    def test_no_photograph_is_referenced_anywhere(self) -> None:
        source = (Path(background.__file__)).read_text(encoding="utf-8")
        for forbidden in (".png", ".jpg", ".jpeg", "media"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_records_belong_to_the_doctor_they_were_built_for(self) -> None:
        for row in (*self.patients, *self.loops, *self.events, *self.relays):
            with self.subTest(row=row.id):
                self.assertEqual(row.doctor_id, DOCTOR)

    def test_seeding_twice_replaces_the_same_twenty(self) -> None:
        """The ids are derived from the doctor, so a second run overwrites."""
        again, _, _, _ = background.records(DOCTOR, NOW)
        self.assertEqual([p.id for p in self.patients], [p.id for p in again])

    def test_two_doctors_do_not_share_a_document(self) -> None:
        other, _, _, _ = background.records("someotherdoctor", NOW)
        self.assertFalse({p.id for p in self.patients} & {p.id for p in other})

    def test_a_monitoring_loop_has_a_summary_worth_reading(self) -> None:
        monitors = [l for l in self.loops if monitoring.is_monitoring(l)]
        self.assertTrue(monitors)
        line = monitoring.summary(monitors[0]).line
        self.assertIn("Expected readings: 14", line)
        self.assertIn("Missing: evenings on days 3, 5 and 6", line)


# The rail can only fire where the document is. The image's build context is
# `app/` alone (deploy.sh runs `gcloud run deploy --source .` from there), so
# docs/RUNBOOK.md is never copied into it and the three tests below errored the
# build with FileNotFoundError: [Errno 2] No such file or directory:
# '/docs/RUNBOOK.md'. They run on the laptop and in any checkout of the whole
# tree, which is where a reworded runbook actually gets written, and skip only
# where the file cannot exist. The fixture's own arithmetic is not skipped.
HAS_RUNBOOK = unittest.skipUnless(RUNBOOK.exists(), "docs/RUNBOOK.md is outside the image")


class TheNumbersTheRunbookPrints(unittest.TestCase):
    def counts(self):
        return background.expected(DOCTOR, NOW)

    def test_the_summary_is_not_trivial(self) -> None:
        counts = self.counts()
        self.assertEqual(counts.carried, 31)
        self.assertEqual(counts.lost, 0)
        for name in ("completed_with_evidence", "progressing", "needs_help",
                     "unreachable", "critical"):
            with self.subTest(bucket=name):
                self.assertGreater(counts.buckets[name], 0)
        self.assertGreater(counts.questions, 0)
        self.assertGreater(counts.attention, 0)

    @HAS_RUNBOOK
    def test_the_runbook_prints_the_line_this_fixture_produces(self) -> None:
        """The rail: the document and the code cannot drift apart."""
        printed = self._printed_line()
        self.assertEqual(printed, summary.line(self.counts()))

    @HAS_RUNBOOK
    def test_the_runbook_prints_the_counts_this_fixture_produces(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        block = text.split('{"carried"', 1)[1].split("```", 1)[0]
        counts = self.counts().as_dict()
        for name in ("completed_with_evidence", "progressing", "needed_help",
                     "unreachable", "questions", "criticals", "attention",
                     "closed_without_evidence", "lost", "duplicates"):
            with self.subTest(count=name):
                found = re.search(rf'"{name}":\s*(\d+)', block)
                self.assertIsNotNone(found, f"{name} is not in the runbook")
                self.assertEqual(int(found.group(1)), counts[name])

    @HAS_RUNBOOK
    def test_the_runbook_names_the_command(self) -> None:
        text = RUNBOOK.read_text(encoding="utf-8")
        self.assertIn('-H "X-Sanad-Admin: $S" "$U/admin/seed-background?name=Test%20Doctor"',
                      text)

    def _printed_line(self) -> str:
        text = RUNBOOK.read_text(encoding="utf-8")
        block = text.split("Today Sanad carried", 1)[1].split("```", 1)[0]
        return " ".join(("Today Sanad carried" + block).split())


class TheSeederWritesRecordsAndNothingElse(unittest.TestCase):
    def setUp(self) -> None:
        self.source = Path(background.__file__).read_text(encoding="utf-8")
        self.main = (Path(background.__file__).resolve().parents[1]
                     / "main.py").read_text(encoding="utf-8")

    def test_it_never_enqueues_a_task_or_sends_a_message(self) -> None:
        for forbidden in ("tasks.enqueue", "fanout(", "telegram.",
                          "OutboundMessage"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_the_route_is_behind_the_admin_secret(self) -> None:
        """The check is a FastAPI dependency now (security audit H1).

        A dependency runs before the body, not inside it, so the guarantee is
        stronger than the call it replaced: there is no line of this function
        that can run before the secret has been compared.
        """
        route = self.main.split("async def seed_background(", 1)[1].split(
            "class PolicyIn", 1)[0]
        signature = self.main.split("async def seed_background(", 1)[1].split(
            "-> dict:", 1)[0]
        self.assertIn("Depends(require_admin)", signature)
        self.assertIn("background.seed(doctor)", route)

    def test_a_doctor_who_is_not_there_is_a_404(self) -> None:
        route = self.main.split("async def seed_background(", 1)[1].split(
            "class PolicyIn", 1)[0]
        self.assertIn('raise HTTPException(404, "Not Found")', route)

    def test_it_says_out_loud_that_they_are_invented(self) -> None:
        self.assertIn("Every one of them is invented", self.source)
        self.assertIn('"synthetic": True', self.source)


if __name__ == "__main__":
    unittest.main()
