"""S18: a reply revives an unreachable loop, and a missing contract is named.

Both halves come from `research/s17-live-results.md`, and both are about the
same thing said twice: an obligation that exists in the doctor's sentence and
nowhere Sanad can act on it.

  1. Defect 3 of that file, whose cause is defect 1 of the reply lane. The
     patient wrote "I'm not doing the test, it's too expensive" and the barrier
     card named the medication. The lipid loop had gone "unreachable" an hour
     earlier, "unreachable" is outside `coordinator.LIVE_STATES`, and
     `coordinator.carrying` therefore could not see the one obligation the
     message was about. The inverse control below is the defect itself: the
     same message, the same board, without the revive, lands on the wrong loop.

  2. Defect 1 of that file. One beat-1 run in five came back with two contracts
     instead of four; the blood-pressure monitoring and the follow-up visit
     were in the plan sentence and in no obligation. `core/duedates.py` cannot
     help, because it fills a date on a loop the model proposed. The card says
     so instead, in the same shape as the two-patient warning, and creates
     nothing.

Nothing here needs Firestore, Telegram, a model or a network.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import chaser, concierge, contract, coordinator, events, registrar
from core import sentinel, store
from core.models import (
    Doctor, Loop, Patient, ProposedLoop, ProposedPatient, ProposedRecord,
)


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

# The runbook's own beat-1 dictation, which is the sentence defect 1 was
# measured on. Four obligations are ordered here in the doctor's own words.
BEAT_ONE = ("Ahmed Ali, 58, male, heart failure and high LDL. Start "
            "atorvastatin 40 at night. Lipid panel in 2 weeks. Blood pressure "
            "twice a day for 7 days. Come back in 3 weeks.")


def doctor() -> Doctor:
    return Doctor(id="d", name="Test Doctor", web_token="token", created_at=NOW)


def patient() -> Patient:
    return Patient(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                   plan_text="Start the tablet at night.", created_at=NOW)


def loop(loop_id: str, kind: str, title: str, state: str, **changes) -> Loop:
    values = dict(id=loop_id, patient_id="p", doctor_id="d", type=kind,
                  title=title, state=state, created_at=NOW, updated_at=NOW)
    values.update(changes)
    return Loop(**values)


def the_board() -> list[Loop]:
    """Beat 2's board at the moment beat 3 is filmed.

    The lipid loop is where the ladder left it: three nudges, no reply,
    "unreachable". The medication is the oldest live loop, which is what
    `carrying` falls back to and what the live card wrongly named.
    """
    return [
        loop("l-med", "MEDICATION", "Start Atorvastatin", "waiting_patient",
             details={"drug": "atorvastatin", "dose": "40 mg"}),
        loop("l-test", "TEST", "Lipid panel", "unreachable",
             details={"test_name": "Lipid panel"}, attempts=3, generation=1),
    ]


class FakeLoops:
    """The loops collection, in memory, with the two writes the revive makes."""

    def __init__(self, rows: list[Loop]) -> None:
        self.rows = {row.id: row for row in rows}

    async def list_loops(self, patient_id: str) -> list[Loop]:
        return [Loop(**row.model_dump()) for row in self.rows.values()
                if row.patient_id == patient_id]

    async def update_loop(self, loop_id: str, **fields) -> None:
        row = self.rows[loop_id]
        self.rows[loop_id] = Loop(**{**row.model_dump(), **fields})

    async def bump_generation(self, loop_id: str) -> int:
        row = self.rows[loop_id]
        self.rows[loop_id] = Loop(**{**row.model_dump(),
                                     "attempts": 0,
                                     "generation": row.generation + 1})
        return self.rows[loop_id].generation


# --------------------------------------------------------------------------- #
# 1a. The revive itself: which loops it touches and what it writes
# --------------------------------------------------------------------------- #
class AReplyBringsAnUnreachableLoopBack(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.board = FakeLoops(the_board())
        self.written: list[SimpleNamespace] = []
        outer = self

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append(SimpleNamespace(
                doctor_id=doctor_id, kind=kind, text=text,
                loop_id=kw.get("loop_id"), meta=kw.get("meta") or {}))
            return SimpleNamespace(id=f"e{len(outer.written)}")

        for target, name, value in (
            (store, "list_loops", self.board.list_loops),
            (store, "update_loop", self.board.update_loop),
            (store, "bump_generation", self.board.bump_generation),
            (store, "now", lambda: NOW),
            (events, "append_event", append_event),
        ):
            self.enterContext(patch.object(target, name, value))

    def feed(self) -> list[str]:
        return [row.text for row in self.written]

    async def test_the_unreachable_loop_goes_back_to_waiting_patient(self) -> None:
        await chaser.revive_unreachable(patient(), doctor())
        self.assertEqual(self.board.rows["l-test"].state, "waiting_patient")

    async def test_the_attempts_are_reset_and_the_generation_goes_up(self) -> None:
        await chaser.revive_unreachable(patient(), doctor())
        revived = self.board.rows["l-test"]
        self.assertEqual(revived.attempts, 0)
        self.assertEqual(revived.generation, 2)

    async def test_the_feed_line_names_the_obligation_and_the_reason(self) -> None:
        await chaser.revive_unreachable(patient(), doctor())
        self.assertEqual(self.feed(),
                         ["Lipid panel back in contact: the patient wrote"])
        self.assertEqual(self.written[0].loop_id, "l-test")

    async def test_the_feed_line_is_written_once(self) -> None:
        """A second message finds the loop live and writes nothing at all."""
        await chaser.revive_unreachable(patient(), doctor())
        await chaser.revive_unreachable(patient(), doctor())
        await chaser.revive_unreachable(patient(), doctor())
        self.assertEqual(len(self.feed()), 1)

    async def test_a_live_loop_is_not_touched(self) -> None:
        before = self.board.rows["l-med"].model_dump()
        await chaser.revive_unreachable(patient(), doctor())
        self.assertEqual(self.board.rows["l-med"].model_dump(), before)

    async def test_a_loop_the_doctor_closed_is_never_revived(self) -> None:
        """"done" is his tap and "pending_review" is his queue. Neither is ours."""
        self.board = FakeLoops([
            loop("l-done", "TEST", "Lipid panel", "done"),
            loop("l-review", "TEST", "Kidney function", "pending_review"),
        ])
        with (patch.object(store, "list_loops", self.board.list_loops),
              patch.object(store, "update_loop", self.board.update_loop),
              patch.object(store, "bump_generation",
                           self.board.bump_generation)):
            revived = await chaser.revive_unreachable(patient(), doctor())
        self.assertEqual(revived, [])
        self.assertEqual(self.board.rows["l-done"].state, "done")
        self.assertEqual(self.board.rows["l-review"].state, "pending_review")
        self.assertEqual(self.feed(), [])


# --------------------------------------------------------------------------- #
# 1b. The lane: the reply reaches the obligation it is about
# --------------------------------------------------------------------------- #
COST = "I'm not doing the test, it's too expensive."


class TheCostBarrierLandsOnTheTest(unittest.IsolatedAsyncioTestCase):
    """Beat 3, on beat 2's board. The Coordinator is asked about one loop.

    The Coordinator itself is stubbed, because what is under test is which loop
    it is handed: its own classification of the barrier was correct live and is
    proved in tests/test_coordinator.py.
    """

    def setUp(self) -> None:
        self.board = FakeLoops(the_board())
        self.carried: list[Loop] = []
        outer = self

        async def append_event(*args, **kwargs):
            return SimpleNamespace(id="e1")

        async def on_patient_reply(loop_row, *args, **kwargs):
            outer.carried.append(loop_row)
            return {"tool": "escalate_barrier"}

        class Recorder:
            def __init__(self) -> None:
                self.sent: list = []

            async def send(self, target, message):
                self.sent.append((target, message))
                return "e"

        self.out = Recorder()
        for target, name, value in (
            (store, "list_loops", self.board.list_loops),
            (store, "update_loop", self.board.update_loop),
            (store, "bump_generation", self.board.bump_generation),
            (store, "now", lambda: NOW),
            (events, "append_event", append_event),
            (concierge, "fanout", lambda: self.out),
            (concierge, "record_reading", AsyncMock(return_value=None)),
            (concierge.coordinator, "on_patient_reply", on_patient_reply),
            (concierge.validator, "model_change_vote",
             AsyncMock(return_value=False)),
            # The administrative tier's code net stays real, because it is the
            # tier this message has to fall through; only its one model vote
            # is stubbed, which is the same stand-down a failed call gives.
            (concierge.intents, "model_vote", AsyncMock(return_value="")),
            (concierge, "answer",
             AsyncMock(side_effect=AssertionError("the model was asked"))),
        ):
            self.enterContext(patch.object(target, name, value))

    def test_without_the_revive_it_lands_on_the_medication(self) -> None:
        """The inverse control: this is the live defect, reproduced in code."""
        chosen = coordinator.carrying(the_board(), COST)
        self.assertEqual(chosen.id, "l-med")

    async def test_the_reply_attaches_to_the_test_loop(self) -> None:
        await concierge.handle_patient_message(
            patient(), doctor(), COST, gate=sentinel.Sentinel())
        self.assertEqual([row.id for row in self.carried], ["l-test"])

    async def test_the_loop_is_live_again_when_routing_reads_it(self) -> None:
        await concierge.handle_patient_message(
            patient(), doctor(), COST, gate=sentinel.Sentinel())
        self.assertEqual(self.carried[0].state, "waiting_patient")


# --------------------------------------------------------------------------- #
# 2. The missing-contract rail on the confirm card
# --------------------------------------------------------------------------- #
FOUR = [
    {"type": "MEDICATION", "title": "Start Atorvastatin",
     "drug": "atorvastatin", "dose": "40 mg", "action": "start"},
    {"type": "TEST", "title": "Lipid panel", "test_name": "Lipid panel",
     "due_in_days": 14},
    {"type": "MONITOR", "title": "Blood pressure monitoring",
     "metric": "Blood pressure", "schedule": "twice a day", "days": 7,
     "due_in_days": 7},
    {"type": "VISIT", "title": "Follow-up visit", "due_in_days": 21},
]


def proposal(loops: list[dict]) -> ProposedRecord:
    return ProposedRecord(
        patient=ProposedPatient(name="Ahmed Ali", age=58, sex="male",
                                diagnosis="heart failure"),
        plan_text="Start the tablet at night and do the tests.",
        loops=[ProposedLoop(**one) for one in loops],
    )


def card_body(loops: list[dict], said: str) -> str:
    card = registrar.confirm_card(proposal(loops), "c1", "Test Doctor",
                                  said=said)
    return " ".join(card["lines"])


class AMissingContractIsNamedOnTheCard(unittest.TestCase):
    def test_the_live_run_that_dropped_two_contracts(self) -> None:
        """Run 1 of five: MEDICATION and TEST only, on the four-part dictation."""
        body = card_body([FOUR[0], FOUR[1]], BEAT_ONE)
        self.assertIn("🔴 Possible missing contract: the dictation mentions "
                      "blood pressure, twice a day but no MONITOR was "
                      "proposed. Cancel and dictate again if it should be "
                      "there.", body)
        self.assertIn("mentions come back but no VISIT was proposed", body)
        self.assertNotIn("no TEST was proposed", body)

    def test_a_missing_monitor(self) -> None:
        body = card_body([FOUR[0], FOUR[1], FOUR[3]], BEAT_ONE)
        self.assertIn("but no MONITOR was proposed", body)
        self.assertNotIn("no VISIT was proposed", body)
        self.assertNotIn("no TEST was proposed", body)

    def test_a_missing_visit(self) -> None:
        body = card_body([FOUR[0], FOUR[1], FOUR[2]], BEAT_ONE)
        self.assertIn("mentions come back but no VISIT was proposed", body)
        self.assertNotIn("no MONITOR was proposed", body)

    def test_a_missing_test(self) -> None:
        body = card_body([FOUR[0], FOUR[2], FOUR[3]], BEAT_ONE)
        self.assertIn("mentions panel but no TEST was proposed", body)
        self.assertNotIn("no MONITOR was proposed", body)
        self.assertNotIn("no VISIT was proposed", body)

    def test_the_dictation_that_got_everything_it_asked_for(self) -> None:
        """The negative: all four ordered, all four proposed, nothing printed."""
        self.assertNotIn("Possible missing contract",
                         card_body(FOUR, BEAT_ONE))

    def test_arabic_words_are_matched_with_the_article_on_the_front(self) -> None:
        body = card_body([FOUR[0]], "احمد علي، قيس الضغط مرتين في اليوم")
        self.assertIn("but no MONITOR was proposed", body)

    def test_an_english_word_inside_another_word_is_not_the_word(self) -> None:
        """"latest" is not "test", which is why the match is space-padded."""
        self.assertNotIn("Possible missing contract",
                         card_body([FOUR[0]], "Ahmed Ali, on his latest dose."))

    def test_a_dictation_that_orders_nothing_prints_nothing(self) -> None:
        self.assertNotIn("Possible missing contract",
                         card_body([FOUR[0]], "Ahmed Ali, 58, male. Start "
                                              "atorvastatin 40 at night."))

    # Round 2. The three false positives measured on the first cut of this
    # rail, each of them an ordinary medication dictation that printed a red
    # MONITOR line about a loop nobody ordered.
    def test_a_dose_frequency_is_not_a_monitoring_order(self) -> None:
        for said in ("Ahmed Ali, 58, male. Start atorvastatin 40 mg daily.",
                     "Ahmed Ali. Stop the metformin, take bisoprolol twice "
                     "a day.",
                     "Ahmed Ali, 60. Weight 95 kg, start metformin 500 mg."):
            with self.subTest(said=said):
                self.assertNotIn("no MONITOR was proposed",
                                 card_body([FOUR[0]], said))

    def test_a_frequency_beside_a_metric_is_a_monitoring_order(self) -> None:
        """The same phrase, in the clause that makes it an order."""
        body = card_body([FOUR[0]],
                         "Ahmed Ali. Blood pressure twice a day for 7 days.")
        self.assertIn("mentions blood pressure, twice a day but no MONITOR "
                      "was proposed", body)

    def test_a_frequency_alone_never_makes_a_type_fire(self) -> None:
        """It can only add the doctor's second phrase to a line already printing."""
        self.assertEqual(
            registrar.missing_contracts(proposal([FOUR[0]]),
                                        "Ahmed Ali. Take it twice a day."),
            [])

    def test_nothing_is_created_by_the_warning(self) -> None:
        record = proposal([FOUR[0]])
        registrar.confirm_card(record, "c1", "Test Doctor", said=BEAT_ONE)
        self.assertEqual([one.type for one in record.loops], ["MEDICATION"])

    def test_the_line_sits_above_the_safety_sentence(self) -> None:
        lines = registrar.confirm_card(proposal([FOUR[0]]), "c1", "Test Doctor",
                                       said=BEAT_ONE)["lines"]
        warned = [i for i, line in enumerate(lines)
                  if "Possible missing contract" in line]
        self.assertTrue(warned)
        self.assertLess(max(warned), lines.index(contract.SAFETY_SENTENCE))

    def test_the_addition_card_carries_the_same_rail(self) -> None:
        existing = SimpleNamespace(id="p", name="Ahmed Ali", age=58,
                                   sex="male", diagnosis="heart failure",
                                   plan_text="", baseline=[], targets=[])
        card = registrar.confirm_card(proposal([FOUR[0]]), "c1", "Test Doctor",
                                      existing=existing, said=BEAT_ONE)
        self.assertIn("Possible missing contract", " ".join(card["lines"]))

    def test_a_card_built_without_the_dictation_warns_about_nothing(self) -> None:
        self.assertNotIn("Possible missing contract",
                         card_body([FOUR[0]], ""))

    def test_the_matched_phrases_are_the_doctors_own_words(self) -> None:
        """Nothing is quoted back that he did not say, and never more than three."""
        said = sentinel.normalize(BEAT_ONE)
        for _kind, phrases in registrar.missing_contracts(
                proposal([FOUR[0]]), BEAT_ONE):
            self.assertLessEqual(len(phrases), registrar.MISSING_CONTRACT_PHRASES)
            for phrase in phrases:
                self.assertTrue(registrar._said_in(said, phrase), phrase)


# --------------------------------------------------------------------------- #
# 3. The dictation travels with the proposal, or two cards in three lose it
# --------------------------------------------------------------------------- #
class TheDictationIsKeptUntilTheTap(unittest.TestCase):
    def test_the_pending_confirm_carries_the_sentence(self) -> None:
        from core.models import PendingConfirm

        confirm = PendingConfirm(id="c1", doctor_id="d", proposed={},
                                 expires_at=NOW, said=BEAT_ONE)
        self.assertEqual(confirm.said, BEAT_ONE)

    def test_an_older_row_without_the_field_still_loads(self) -> None:
        from core.models import PendingConfirm

        confirm = PendingConfirm(id="c1", doctor_id="d", proposed={},
                                 expires_at=NOW)
        self.assertEqual(confirm.said, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
