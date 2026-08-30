"""S17: the due date the model forgot, read back out of the doctor's sentence.

research/s16-live-results.md defect 2, measured on revision 26: the runbook's
own beat-1 dictation came back from Gemini with `due_in_days` empty on the TEST
loop and on the MONITOR loop, twice in a row, for two different patients. The
confirm card then said "no due date was dictated" about a sentence that says
"in 2 weeks", Confirm queued nine follow-up tasks instead of twelve, and the
lipid ladder the video points at never ran.

Three halves, for the reason the rest of this package has them:

  1. core/duedates.py is pure and runs anywhere with nothing installed. The
     phrases, the attachment rule and the refusals are proved there.
  2. The rail reads the beat-1 sentence out of docs/RUNBOOK.md rather than
     typing it here, so rewording the beat without teaching the reader fails
     this file instead of failing on camera. It skips inside the image, where
     docs/ is not in the build context.
  3. The end-to-end half drives the Registrar and the real chaser against an
     in-memory board, so it reaches the cloud SDK and skips on a laptop with
     none. It is what proves the feed line: twelve tasks, counted from what was
     actually enqueued.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from core import duedates
from core.models import ProposedLoop, ProposedPatient, ProposedRecord

from tests import Borrowable
from tests.test_wave_c import HAS_RUNBOOK, runbook_beat_one

try:  # the same gate tests/test_identify.py uses, for the same reason
    from core import chaser, identify, links, registrar, store as store_module
    from core import telegram
    from core.models import Doctor
    from tests.test_identify import (FakeStore, NOW, STORE_NAMES, verdict)
    import core.events as events_module
    SDK_MISSING = ""
except Exception as exc:  # pragma: no cover
    SDK_MISSING = f"the Registrar is not importable here: {exc}"


# The dictation, and the four loops the Registrar wrote for it on revision 26,
# with the three due dates exactly as empty as they came back live.
BEAT_ONE = ("Ahmed Ali, 58, male, heart failure and high LDL. Start "
            "atorvastatin 40 at night. Lipid panel in 2 weeks. Blood pressure "
            "twice a day for 7 days. Come back in 3 weeks.")

BEAT_ONE_LOOPS = [
    {"type": "MEDICATION", "title": "Start atorvastatin 40 mg at night",
     "drug": "atorvastatin", "dose": "40 mg", "action": "start"},
    {"type": "TEST", "title": "Lipid panel", "test_name": "Lipid panel"},
    {"type": "MONITOR", "title": "Blood pressure monitoring",
     "metric": "Blood pressure", "schedule": "twice a day", "days": 7},
    {"type": "VISIT", "title": "Follow-up visit"},
]


def record(loops: list[dict], name: str = "Ahmed Ali") -> ProposedRecord:
    return ProposedRecord(
        patient=ProposedPatient(name=name, age=58, sex="male",
                                diagnosis="heart failure"),
        plan_text="Start the tablet at night and do the tests.",
        loops=[ProposedLoop(**one) for one in loops],
    )


def dates(filled: ProposedRecord) -> list:
    return [loop.due_in_days for loop in filled.loops]


# --------------------------------------------------------------------------- #
# 1. The phrases, in the three ways a doctor in Egypt says them
# --------------------------------------------------------------------------- #
class TheRelativePhrasesADoctorSays(unittest.TestCase):
    def test_english(self) -> None:
        for said, days in (("in 2 weeks", 14), ("in two weeks", 14),
                           ("after 3 weeks", 21), ("in 10 days", 10),
                           ("in a month", 30), ("in one month", 30),
                           ("after ten days", 10), ("2 weeks from now", 14),
                           ("three days from now", 3), ("next week", 7),
                           ("next month", 30)):
            with self.subTest(said=said):
                self.assertEqual(duedates.days_in(said), [days])

    def test_arabic(self) -> None:
        for said, days in (("بعد اسبوعين", 14), ("بعد أسبوعين", 14),
                           ("كمان اسبوع", 7), ("بعد 3 اسابيع", 21),
                           ("بعد ٣ ايام", 3), ("بعد شهر", 30),
                           ("بعد شهرين", 60), ("بعد يومين", 2),
                           ("الاسبوع الجاي", 7), ("الشهر الجاي", 30)):
            with self.subTest(said=said):
                self.assertEqual(duedates.days_in(said), [days])

    def test_franco(self) -> None:
        for said, days in (("ba3d osbo3en", 14), ("kaman 2 weeks", 14),
                           ("kaman osbo3", 7), ("ba3d talat ayam", 3),
                           ("ba3d shahr", 30), ("ba3d yomen", 2)):
            with self.subTest(said=said):
                self.assertEqual(duedates.days_in(said), [days])

    def test_a_duration_is_not_a_deadline(self) -> None:
        """"for 7 days" says how long, not when. Neither does a dose or a time."""
        for said in ("Blood pressure twice a day for 7 days",
                     "twice a day", "Start atorvastatin 40 at night",
                     "come back when the results are ready",
                     "take it every day", "40 mg at night",
                     "لما تجيب النتيجة ابعتهالي", "مرتين في اليوم لمدة 7 ايام"):
            with self.subTest(said=said):
                self.assertEqual(duedates.days_in(said), [])

    def test_a_number_with_no_unit_says_nothing(self) -> None:
        for said in ("in 2", "after three", "Ahmed Ali, 58, male", "in the morning"):
            with self.subTest(said=said):
                self.assertEqual(duedates.days_in(said), [])


# --------------------------------------------------------------------------- #
# 2. Which loop the phrase belongs to, and when nothing is filled at all
# --------------------------------------------------------------------------- #
class TheDateIsAttachedOrNothingIs(unittest.TestCase):
    def test_the_beat_one_dictation_fills_all_three(self) -> None:
        """The live defect, with the model's answer exactly as it came back."""
        filled, found = duedates.fill(record(BEAT_ONE_LOOPS), BEAT_ONE)
        self.assertEqual(dates(filled), [None, 14, 7, 21])
        self.assertEqual(found[1][1], duedates.FROM_DICTATION)
        self.assertEqual(found[2][1], duedates.FROM_MONITOR_DAYS)
        self.assertEqual(found[3][1], duedates.FROM_DICTATION)

    def test_the_medication_loop_is_never_given_a_deadline(self) -> None:
        filled, found = duedates.fill(record(BEAT_ONE_LOOPS), BEAT_ONE)
        self.assertIsNone(filled.loops[0].due_in_days)
        self.assertNotIn(0, found)

    def test_a_date_the_model_returned_is_never_touched(self) -> None:
        loops = [dict(BEAT_ONE_LOOPS[1], due_in_days=30)]
        filled, found = duedates.fill(record(loops), BEAT_ONE)
        self.assertEqual(dates(filled), [30])
        self.assertEqual(found, {})

    def test_the_original_record_is_not_mutated(self) -> None:
        original = record(BEAT_ONE_LOOPS)
        duedates.fill(original, BEAT_ONE)
        self.assertEqual(dates(original), [None, None, None, None])

    def test_a_visit_is_recognised_by_the_words_that_order_one(self) -> None:
        """"Come back in 3 weeks" shares no word with "Follow-up visit"."""
        visit = [BEAT_ONE_LOOPS[3]]
        for said, days in (("Come back in 3 weeks", 21),
                           ("Follow up in 10 days", 10),
                           ("تعالى تاني بعد اسبوعين", 14),
                           ("erga3li ba3d shahr", 30)):
            with self.subTest(said=said):
                filled, _ = duedates.fill(record(visit), said)
                self.assertEqual(dates(filled), [days])

    def test_a_test_is_recognised_by_its_own_name_or_by_the_word_test(self) -> None:
        test = [BEAT_ONE_LOOPS[1]]
        for said, days in (("Lipid panel in 2 weeks", 14),
                           ("3amel lipid panel ba3d osbo3en", 14),
                           ("اعمل التحليل كمان اسبوع", 7),
                           ("e3mel el ta7lil kaman 2 weeks", 14)):
            with self.subTest(said=said):
                filled, _ = duedates.fill(record(test), said)
                self.assertEqual(dates(filled), [days])

    def test_a_phrase_that_names_nothing_attaches_to_nothing(self) -> None:
        """The negative the brief names: no phrase, so no date, and no guess."""
        filled, found = duedates.fill(
            record([BEAT_ONE_LOOPS[3]]),
            "Ahmed Ali, 58, male. Come back when the results are ready.")
        self.assertEqual(dates(filled), [None])
        self.assertEqual(found, {})

    def test_one_clause_that_could_be_two_loops_fills_neither(self) -> None:
        """Two obligations in one breath is not an answer about either of them."""
        loops = [BEAT_ONE_LOOPS[1], BEAT_ONE_LOOPS[3]]
        filled, found = duedates.fill(
            record(loops), "Lipid panel and come back in 2 weeks")
        self.assertEqual(dates(filled), [None, None])
        self.assertEqual(found, {})

    def test_two_different_phrases_in_one_clause_fill_nothing(self) -> None:
        filled, _ = duedates.fill(
            record([BEAT_ONE_LOOPS[1]]),
            "Lipid panel in 2 weeks or in a month")
        self.assertEqual(dates(filled), [None])

    def test_a_monitor_with_no_days_and_no_phrase_stays_empty(self) -> None:
        loops = [{"type": "MONITOR", "title": "Blood pressure monitoring",
                  "metric": "Blood pressure", "schedule": "twice a day"}]
        filled, _ = duedates.fill(record(loops), "Blood pressure twice a day")
        self.assertEqual(dates(filled), [None])

    def test_a_phrase_beats_the_monitoring_duration(self) -> None:
        loops = [dict(BEAT_ONE_LOOPS[2])]
        filled, found = duedates.fill(
            record(loops),
            "Blood pressure twice a day for 7 days. Send me the blood pressure "
            "readings in 10 days.")
        self.assertEqual(dates(filled), [10])
        self.assertEqual(found[0][1], duedates.FROM_DICTATION)

    def test_a_date_further_out_than_the_cap_is_not_filled(self) -> None:
        """The same outer bound `registrar.validate` refuses, applied first."""
        filled, _ = duedates.fill(
            record([BEAT_ONE_LOOPS[1]]), "Lipid panel in 400 days", cap=365)
        self.assertEqual(dates(filled), [None])

    def test_an_empty_dictation_fills_nothing(self) -> None:
        filled, found = duedates.fill(record(BEAT_ONE_LOOPS), "")
        self.assertEqual(dates(filled), [None, None, 7, None])
        self.assertEqual(found[2][1], duedates.FROM_MONITOR_DAYS)


# --------------------------------------------------------------------------- #
# 3. The rail: the document and the fallback cannot drift apart
# --------------------------------------------------------------------------- #
class TheRunbookSentenceIsTheOneTheCodeReads(unittest.TestCase):
    @HAS_RUNBOOK
    def test_the_beat_one_sentence_yields_fourteen_seven_and_twenty_one(
            self) -> None:
        """Read out of docs/RUNBOOK.md 1b, not typed here.

        Every `due_in_days` is blanked first, which is exactly what the model
        returned live on revision 26 for two of the three, so this is the
        defect's own input.
        """
        said = runbook_beat_one()
        self.assertIn("Lipid panel in 2 weeks", said)
        filled, found = duedates.fill(record(BEAT_ONE_LOOPS), said)
        self.assertEqual(dates(filled), [None, 14, 7, 21])
        self.assertEqual(len(found), 3)


# --------------------------------------------------------------------------- #
# 4. What the card says when it could not read one
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheCardPrintsTheDeadlineItCouldNotRead(unittest.TestCase):
    def test_a_loop_with_no_due_date_is_named_in_the_not_dictated_block(
            self) -> None:
        card = registrar.confirm_card(record([BEAT_ONE_LOOPS[3]]), "c1",
                                      "Dr Mohamed")
        body = " ".join(card["lines"])
        self.assertIn("Not dictated, and NOT filled in by Sanad:", body)
        self.assertIn(registrar.DUE_MISSING, body)

    def test_the_line_sits_above_the_safety_sentence(self) -> None:
        from core import contract

        body = registrar.confirm_card(record([BEAT_ONE_LOOPS[3]]), "c1",
                                      "Dr Mohamed")["lines"]
        missing_at = [i for i, line in enumerate(body)
                      if registrar.DUE_MISSING in line][0]
        self.assertLess(missing_at, body.index(contract.SAFETY_SENTENCE))

    def test_a_filled_date_is_not_reported_as_missing(self) -> None:
        filled, _ = duedates.fill(record(BEAT_ONE_LOOPS), BEAT_ONE)
        body = " ".join(registrar.confirm_card(filled, "c1", "Dr Mohamed")["lines"])
        self.assertNotIn(registrar.DUE_MISSING, body)
        self.assertIn("due in 14d", body)
        self.assertIn("due in 21d", body)

    def test_a_medication_without_a_date_is_not_reported_as_missing(self) -> None:
        body = " ".join(registrar.confirm_card(
            record([BEAT_ONE_LOOPS[0]]), "c1", "Dr Mohamed")["lines"])
        self.assertNotIn(registrar.DUE_MISSING, body)


# --------------------------------------------------------------------------- #
# 5. End to end: the dictation, the ladder, and the number on the feed line
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheFeedLineCountsWhatWasQueued(Borrowable):
    """The whole of defect 2, from the doctor's words to the number he reads.

    Nothing here is a stub except the two model calls: `registrar.propose`
    returns the record revision 26 actually returned, with the three due dates
    empty, and the identification returns a new patient. The chaser is the real
    one and only the queue underneath it is captured, so the twelve is counted
    the way Cloud Tasks would count it.
    """

    def setUp(self) -> None:
        outer = self
        self.fake = FakeStore()
        self.sent: list = []
        self.queued: list = []
        self.proposed = {
            "patient": {"name": "Ahmed Ali", "age": 58, "sex": "male",
                        "diagnosis": "heart failure and high LDL"},
            "baseline": [], "targets": [],
            "plan_text": "Start the tablet at night, do the lipid panel, "
                         "measure your pressure and come back.",
            "loops": BEAT_ONE_LOOPS,
        }
        self.doctor = Doctor(id="d", name="Dr Mohamed", specialty="cardiology",
                             lang="en", web_token="goodtoken",
                             telegram_chat_id=None, created_at=NOW)
        self.fake.doctors["d"] = self.doctor

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append(msg)
                meta = dict(msg.meta or {})
                if msg.card:
                    meta["card"] = msg.card
                written = await events_module.append_event(
                    outer.doctor.id, "card" if msg.card else "agent_out",
                    msg.text, patient_id=msg.patient_id, meta=meta)
                return written.id

        async def propose(text):
            return ProposedRecord.model_validate(outer.proposed)

        async def identify_stub(text, rows, extracted_name=""):
            return verdict("new_patient")

        async def mint(doctor, patient, token_id=""):
            from types import SimpleNamespace

            return SimpleNamespace(id="tok1", patient_id=patient.id)

        async def card_lines(token, base_url=""):
            return ["link: https://example/p/tok1"]

        async def enqueue(path, payload, delay):
            outer.queued.append(payload)
            return f"task/{len(outer.queued)}"

        async def current_settings():
            return "run1", 86400

        for name in STORE_NAMES:
            self.enterContext(patch.object(store_module, name,
                                           getattr(self.fake, name)))
        self.enterContext(patch.object(registrar, "fanout", lambda: Fanout()))
        self.enterContext(patch.object(registrar, "propose", propose))
        self.enterContext(patch.object(identify, "identify", identify_stub))
        self.enterContext(patch.object(links, "mint", mint))
        self.enterContext(patch.object(links, "card_lines", card_lines))
        self.enterContext(patch.object(chaser.tasks, "enqueue", enqueue))
        self.enterContext(patch.object(chaser.settings, "current",
                                       current_settings))
        self.enterContext(patch.object(telegram, "enabled", lambda: False))

    def confirm_id(self) -> str:
        return list(self.fake.confirms)[0]

    def feed(self) -> list[str]:
        return [event.text for event in self.fake.events.values()]

    async def commit_beat_one(self) -> None:
        await registrar.handle_doctor(self.doctor, BEAT_ONE)
        await registrar.commit(self.doctor, self.confirm_id())

    async def test_the_three_loops_are_committed_with_the_dictated_dates(
            self) -> None:
        await self.commit_beat_one()
        due = {loop.type: loop.due_at for loop in self.fake.loops.values()}
        self.assertIsNone(due["MEDICATION"])
        self.assertEqual(due["TEST"] - self.fake.clock, timedelta(days=14))
        self.assertEqual(due["MONITOR"] - self.fake.clock, timedelta(days=7))
        self.assertEqual(due["VISIT"] - self.fake.clock, timedelta(days=21))

    async def test_the_feed_line_is_twelve_and_it_is_what_was_enqueued(
            self) -> None:
        """The number the runbook promises, from the sentence the runbook uses.

        On revision 26 this line read "9 follow-up tasks scheduled" and the
        lipid ladder was the three that were missing.
        """
        await self.commit_beat_one()
        self.assertEqual(len(self.queued), 12)
        self.assertIn(f"{len(self.queued)} follow-up tasks scheduled", self.feed())
        self.assertEqual(sum(1 for p in self.queued if p["kind"] == "monitor"), 6)
        self.assertEqual(sum(1 for p in self.queued if p["kind"] == "nudge"), 6)

    async def test_the_line_counts_the_queue_and_not_the_loops(self) -> None:
        """A queue that refuses is a smaller number, never a wrong one."""
        async def refuse(path, payload, delay):
            raise RuntimeError("cloud tasks is down")

        with patch.object(chaser.tasks, "enqueue", refuse):
            await self.commit_beat_one()
        self.assertIn("0 follow-up tasks scheduled", self.feed())
        self.assertIn("record committed", self.feed())

    async def test_the_card_the_doctor_taps_already_carries_the_dates(
            self) -> None:
        await registrar.handle_doctor(self.doctor, BEAT_ONE)
        card = [msg.card for msg in self.sent if msg.card][-1]
        body = " ".join(card["lines"])
        self.assertIn("due in 14d", body)
        self.assertIn("due in 7d", body)
        self.assertIn("due in 21d", body)
        self.assertNotIn(registrar.DUE_MISSING, body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
