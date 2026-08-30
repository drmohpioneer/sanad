"""The administrative tier: the six chores, and the six things they must not do.

S6++ item G. Each intent is proved in Arabic and in English, each has a negative
that reads like it and is not it, and the whole tier is proved to stand down
rather than improvise: no obligation of the right kind, no day named in a
reschedule, or a guard in core/policy.py refusing, and the message carries on
down the tiers exactly as it did before this file existed.

The detection half is pure and runs anywhere. The half that drives the tier
imports core/coordinator.py, which reaches the cloud SDK, so it skips on a
laptop with none and runs in the image.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core import intents, sentinel, timing, validator

from tests import Borrowable

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)  # a Saturday in Cairo
RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md"
# The image's build context is app/ alone, so docs/ is not copied into it and
# the rail below skips there and runs everywhere the document can be edited.
HAS_RUNBOOK = unittest.skipUnless(
    RUNBOOK.exists(), "docs/RUNBOOK.md is outside the image")

# Each intent, said the way a patient says it: Egyptian Arabic, English, and
# where it is natural, Franco-Arabic.
SAID: dict[str, tuple[str, ...]] = {
    intents.DID_TEST: (
        "عملت التحليل امبارح",
        "I did the test yesterday",
        "3amalt el ta7lil embare7",
    ),
    intents.LOST_PRESCRIPTION: (
        "ضيعت الروشتة",
        "I lost my prescription",
        "da3et el roshetta",
    ),
    intents.RESCHEDULE_VISIT: (
        "ممكن أجي الخميس بدل الأربع؟",
        "can I come on Thursday instead of the appointment?",
    ),
    intents.WHERE_TO_SEND: (
        "أبعتها فين؟",
        "where do I send it?",
    ),
    intents.MEDICINE_UNAVAILABLE: (
        "الدوا مش متوفر في الصيدلية",
        "the medicine is not available at the pharmacy",
    ),
    intents.FORGOT_MEASURE: (
        "نسيت أقيس الضغط النهاردة",
        "I forgot to measure my blood pressure this morning",
    ),
}

# One per intent that reads like it and is not it.
NOT_SAID: dict[str, tuple[str, ...]] = {
    intents.DID_TEST: ("هعمل التحليل بكرة", "I will do the test tomorrow"),
    intents.LOST_PRESCRIPTION: ("الروشتة معايا",
                                "I have the prescription with me"),
    intents.RESCHEDULE_VISIT: ("الميعاد إمتى؟", "when is my appointment?"),
    intents.WHERE_TO_SEND: ("النتيجة طلعت كويسة؟", "what does the result mean?"),
    intents.MEDICINE_UNAVAILABLE: ("الدوا غالي", "the medicine is expensive"),
    intents.FORGOT_MEASURE: ("قست الضغط النهاردة",
                             "I measured my blood pressure today"),
}


class TheSixAreRecognised(unittest.TestCase):
    def test_each_intent_in_arabic_and_in_english(self) -> None:
        for intent, messages in SAID.items():
            for message in messages:
                with self.subTest(intent=intent, message=message):
                    self.assertEqual(intents.match(message), intent)

    def test_one_negative_per_intent(self) -> None:
        for intent, messages in NOT_SAID.items():
            for message in messages:
                with self.subTest(intent=intent, message=message):
                    self.assertEqual(intents.match(message), "")

    def test_a_greeting_a_thank_you_and_nothing_are_not_intents(self) -> None:
        for message in ("شكرا يا دكتور", "hello", "", "   ", "128/84"):
            with self.subTest(message=message):
                self.assertEqual(intents.match(message), "")

    def test_the_six_are_the_six_the_spec_names(self) -> None:
        self.assertEqual(set(intents.INTENTS), {
            "did_test", "lost_prescription", "reschedule_visit",
            "where_to_send", "medicine_unavailable", "forgot_measure"})
        self.assertEqual(set(intents.PATTERNS), set(intents.INTENTS))


class TheTestNamedInTheMiddle(unittest.TestCase):
    """S15 defect 1. "I did the glucose test" is the sentence, not "the test".

    Found live twice from a real patient box: the substring list carried "i did
    the test" and the patient named the test in the middle of it, so net one
    matched nothing, the add-only vote may not name a state-changing intent,
    and the whole administrative tier stood down. The refusal beat never
    happened. core/intents.PATTERN_RES is the fix and this is its boundary.
    """

    def test_the_test_can_be_named_in_the_middle(self) -> None:
        for message in ("I did the glucose test", "I did the lipid test",
                        "did the blood test", "I did my glucose test",
                        "I did the glucose tolerance test",
                        "I did the glucose test yesterday",
                        "عملت تحليل السكر", "عملت تحليل الدهون",
                        "3amalt ta7lil el sokar", "3amalt ta7alil el dam"):
            with self.subTest(message=message):
                self.assertEqual(intents.match(message), intents.DID_TEST)

    def test_a_refusal_and_a_promise_are_still_not_a_done_test(self) -> None:
        """The article is required, which is what keeps every negation out."""
        for message in ("I did not do the test", "I didn't do the test",
                        "I did not do the glucose test",
                        "I will do the glucose test tomorrow",
                        "I have not done the glucose test",
                        "هعمل تحليل السكر بكرة"):
            with self.subTest(message=message):
                self.assertEqual(intents.match(message), "")

    def test_the_expression_cannot_reach_across_a_sentence(self) -> None:
        """At most three words in the gap, and a test done TO him is not a done
        test, so neither of these is an administrative chore."""
        for message in ("I did the shopping and my wife had a test",
                        "did the doctor test me for diabetes"):
            with self.subTest(message=message):
                self.assertEqual(intents.match(message), "")

    def test_the_reading_can_be_named_in_the_middle_too(self) -> None:
        for message in ("I forgot to measure my pressure",
                        "I forgot to check my blood pressure",
                        "I forgot to take my morning blood pressure",
                        "I forgot to record my sugar"):
            with self.subTest(message=message):
                self.assertEqual(intents.match(message), intents.FORGOT_MEASURE)

    def test_a_forgotten_anything_else_is_not_a_forgotten_reading(self) -> None:
        for message in ("I forgot to take my medicine",
                        "I forgot to book the appointment",
                        "I did not forget to measure my pressure"):
            with self.subTest(message=message):
                self.assertNotEqual(intents.match(message),
                                    intents.FORGOT_MEASURE)

    def test_every_expression_belongs_to_an_intent_that_exists(self) -> None:
        self.assertLessEqual(set(intents.PATTERN_RES), set(intents.INTENTS))

    @HAS_RUNBOOK
    def test_the_runbook_beat_7_sentence_is_the_one_the_code_matches(self) -> None:
        """The rail: the document and the pattern list cannot drift apart.

        The sentence is read out of docs/RUNBOOK.md 1b beat 7, not typed here,
        so rewording the beat without teaching the matcher fails this test
        instead of failing on camera.
        """
        text = RUNBOOK.read_text(encoding="utf-8")
        after = text.split("**Beat 7, the refusal:**", 1)
        self.assertEqual(len(after), 2, "beat 7 is not in the runbook")
        said = after[1].split("```", 2)[1].strip()
        self.assertEqual(said, "I did the glucose test")
        self.assertEqual(intents.match(said), intents.DID_TEST)
        self.assertEqual(intents.action_for(
            intents.DID_TEST, None, said, NOW)[0], "schedule_next_contact")


class TheGatesInFrontOfItStillDecide(unittest.TestCase):
    """An intent is only ever reached by a message the gates above passed."""

    def test_no_intent_message_is_a_treatment_change_in_disguise(self) -> None:
        for messages in SAID.values():
            for message in messages:
                with self.subTest(message=message):
                    self.assertFalse(validator.wants_treatment_change(message))

    def test_no_intent_message_is_an_emergency(self) -> None:
        for messages in SAID.values():
            for message in messages:
                with self.subTest(message=message):
                    self.assertIsNone(sentinel.code_net(message))

    def test_an_emergency_wearing_an_intent_is_still_an_emergency(self) -> None:
        """The Sentinel runs first and this is the message that proves why."""
        message = "عملت التحليل بس عندي وجع فظيع بمنتصف الصدر ونازل لدراعي الشمال"
        self.assertEqual(intents.match(message), intents.DID_TEST)
        self.assertIsNotNone(sentinel.code_net(message))


class TheDayThePatientAskedFor(unittest.TestCase):
    def test_a_weekday_is_read_in_both_languages(self) -> None:
        for message, weekday in (("ممكن أجي الخميس", 3),
                                 ("can I come on thursday", 3),
                                 ("ممكن أجي الأحد بدل كده", 6),
                                 ("can I come on Monday instead", 0)):
            with self.subTest(message=message):
                self.assertEqual(intents.weekday_in(message), weekday)

    def test_a_message_with_no_day_names_none(self) -> None:
        self.assertIsNone(intents.weekday_in("can I come another day instead"))

    def test_the_next_such_day_is_never_today(self) -> None:
        saturday = NOW  # 2026-08-29 is a Saturday
        self.assertEqual(intents.days_until(saturday, 5), 7)   # Saturday again
        self.assertEqual(intents.days_until(saturday, 6), 1)   # Sunday
        self.assertEqual(intents.days_until(saturday, 3), 5)   # Thursday

    def test_a_reschedule_with_no_day_in_it_stands_down(self) -> None:
        """Sanad does not invent a date for "another day"."""
        self.assertIsNone(intents.action_for(
            intents.RESCHEDULE_VISIT, None, "can I come another day instead",
            NOW))

    def test_every_other_intent_names_its_tool(self) -> None:
        wanted = {
            intents.DID_TEST: "schedule_next_contact",
            intents.FORGOT_MEASURE: "classify_barrier",
            intents.MEDICINE_UNAVAILABLE: "escalate_barrier",
        }
        for intent, tool in wanted.items():
            with self.subTest(intent=intent):
                action = intents.action_for(intent, None, "", NOW)
                self.assertIsNotNone(action)
                self.assertEqual(action[0], tool)

    def test_an_answering_intent_has_no_tool_at_all(self) -> None:
        for intent in intents.ANSWER_ONLY:
            with self.subTest(intent=intent):
                self.assertIsNone(intents.action_for(intent, None, "", NOW))


# The rest drives the tier, which reaches core/coordinator.py and the SDK.
try:
    from core import concierge, coordinator, intents as intents_module  # noqa: F401
    from core.models import Doctor, Loop, Patient
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class WhichObligationAnIntentIsAbout(unittest.TestCase):
    def board(self) -> list:
        def loop(kind, title, minutes, state="waiting_patient"):
            made = NOW + timedelta(minutes=minutes)
            return Loop(id=title, patient_id="p", doctor_id="d", type=kind,
                        title=title, state=state, created_at=made,
                        updated_at=made)

        return [loop("MONITOR", "Blood pressure monitoring", 0),
                loop("TEST", "Lipid panel", 1),
                loop("VISIT", "Follow-up visit", 2, state="open"),
                loop("MEDICATION", "Start Atorvastatin", 3, state="open")]

    def test_each_intent_lands_on_its_own_kind_of_obligation(self) -> None:
        board = self.board()
        wanted = {
            intents.DID_TEST: "Lipid panel",
            intents.RESCHEDULE_VISIT: "Follow-up visit",
            intents.MEDICINE_UNAVAILABLE: "Start Atorvastatin",
            intents.FORGOT_MEASURE: "Blood pressure monitoring",
        }
        for intent, title in wanted.items():
            with self.subTest(intent=intent):
                loop = intents.loop_for(intent, board, SAID[intent][0])
                self.assertIsNotNone(loop)
                self.assertEqual(loop.title, title)

    def test_no_obligation_of_that_kind_is_a_stand_down(self) -> None:
        board = [l for l in self.board() if l.type != "TEST"]
        self.assertIsNone(intents.loop_for(intents.DID_TEST, board, "I did the test"))

    def test_a_paused_or_closed_obligation_is_not_it(self) -> None:
        board = self.board()
        for loop in board:
            loop.paused = True
        self.assertIsNone(intents.loop_for(intents.DID_TEST, board, ""))


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheTierActsThroughTheGuardedTools(Borrowable):
    """What each intent actually does, with everything around it faked."""

    def setUp(self) -> None:
        from core import adapters, events as events_module, lang, settings
        from core import store as store_module
        from core import tasks as tasks_module

        outer = self
        self.sent: list = []
        self.written: list = []
        self.queued: list = []
        self.relays: dict = {}
        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", plan_text="Take one tablet at night.",
                               created_at=NOW)

        def loop(kind, title, minutes, state="waiting_patient", **extra):
            made = NOW + timedelta(minutes=minutes)
            return Loop(id=title, patient_id="p", doctor_id="d", type=kind,
                        title=title, state=state, created_at=made,
                        updated_at=made, **extra)

        self.test_loop = loop("TEST", "Lipid panel", 1,
                              due_at=NOW + timedelta(days=10))
        self.visit = loop("VISIT", "Follow-up visit", 2, state="open",
                          due_at=NOW + timedelta(days=14))
        self.monitor = loop("MONITOR", "Blood pressure monitoring", 0,
                            details={"metric": "BP", "schedule": "twice a day",
                                     "days": 7})
        self.medication = loop("MEDICATION", "Start Atorvastatin", 3,
                               state="open", details={"drug": "atorvastatin"})
        self.board = [self.monitor, self.test_loop, self.visit, self.medication]
        self.by_id = {l.id: l for l in self.board}

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card, msg.meta))

        async def update_loop(loop_id, **fields):
            for key, value in fields.items():
                setattr(outer.by_id[loop_id], key, value)

        async def bump_schedule_version(loop_id):
            loop = outer.by_id[loop_id]
            loop.schedule_version = int(loop.schedule_version or 0) + 1
            return loop.schedule_version

        async def get_loop(loop_id):
            return outer.by_id.get(loop_id)

        async def get_patient(patient_id):
            return outer.patient

        async def save_relay(relay):
            outer.relays[relay.id] = relay
            return relay

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append((kind, text, kw.get("meta", {})))
            return None

        async def enqueue(path, payload, delay):
            outer.queued.append((path, payload, delay))
            return f"task/{len(outer.queued)}"

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "ar"

        async def no_vote(text):
            raise AssertionError("the model vote was asked after a code match")

        # The wave B store surface: the patient-wide contact ledger and the
        # counters that are server-side increments in core/store.py.
        ledger: list = []

        async def add_contact(loop_id, day_index):
            loop = outer.by_id[loop_id]
            loop.contacts = int(loop.contacts or 0) + 1
            if day_index not in (loop.contact_days or []):
                loop.contact_days = [*(loop.contact_days or []), day_index]

        async def add_reluctance(loop_id):
            loop = outer.by_id[loop_id]
            loop.reluctance = int(loop.reluctance or 0) + 1
            return loop.reluctance

        async def claim_resume(loop_id, note):
            loop = outer.by_id[loop_id]
            if not (loop.paused or loop.barrier):
                return False
            loop.paused = False
            loop.barrier = ""
            loop.barrier_note = note
            return True

        async def note_contact(patient_id, doctor_id, day_index, kind,
                               loop_id=""):
            ledger.append((patient_id, day_index, kind))
            return len(ledger)

        async def contacted_on(patient_id, day_index):
            return any(row[0] == patient_id and row[1] == day_index
                       for row in ledger)

        async def contact_days_for_patient(patient_id):
            return tuple(row[1] for row in ledger if row[0] == patient_id)

        self.patches = [
            # core/intents.py imports the adapter inside the function that
            # sends, so that its pattern list runs with nothing installed.
            patch.object(adapters, "fanout", lambda: Fanout()),
            patch.object(coordinator, "fanout", lambda: Fanout()),
            patch.object(concierge, "fanout", lambda: Fanout()),
            patch.object(store_module, "update_loop", update_loop),
            patch.object(store_module, "add_contact", add_contact),
            patch.object(store_module, "add_reluctance", add_reluctance),
            patch.object(store_module, "claim_resume", claim_resume),
            patch.object(store_module, "note_contact", note_contact),
            patch.object(store_module, "contacted_on", contacted_on),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
            patch.object(store_module, "bump_schedule_version",
                         bump_schedule_version),
            patch.object(store_module, "get_loop", get_loop),
            patch.object(store_module, "get_patient", get_patient),
            patch.object(store_module, "save_relay", save_relay),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(events_module, "append_event", append_event),
            patch.object(tasks_module, "enqueue", enqueue),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
            patch.object(intents, "model_vote", no_vote),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def handle(self, message: str):
        return await intents.handle(self.patient, self.doctor, message,
                                    self.board)

    def to_patient(self) -> list:
        return [text for ref, text, _, _ in self.sent if ref.startswith("patient:")]

    def cards(self) -> list:
        return [card for _, _, card, _ in self.sent if card]

    async def test_i_did_the_test_asks_for_the_photo_and_waits_for_it(self) -> None:
        from core import templates

        result = await self.handle("عملت التحليل امبارح")
        self.assertIsNotNone(result)
        self.assertEqual(result["intent"], intents.DID_TEST)
        self.assertEqual(self.test_loop.state, "waiting_patient")
        self.assertEqual(result["detail"]["state"], "waiting_patient")
        self.assertEqual(self.to_patient(),
                         [templates.TEMPLATES["send_when_ready"]["ar"]["m"]])
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.cards(), [])

    async def test_i_did_the_test_in_english_does_the_same_thing(self) -> None:
        result = await self.handle("I did the test yesterday")
        self.assertEqual(result["intent"], intents.DID_TEST)
        self.assertEqual(result["tool"], "schedule_next_contact")

    async def test_i_lost_the_prescription_sends_the_plan_again(self) -> None:
        result = await self.handle("ضيعت الروشتة")
        self.assertEqual(result["intent"], intents.LOST_PRESCRIPTION)
        said = self.to_patient()
        self.assertEqual(len(said), 1)
        self.assertIn("Take one tablet at night.", said[0])
        self.assertIn("Test Doctor", said[0])
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.queued, [])

    async def test_a_lost_prescription_with_no_plan_stands_down(self) -> None:
        self.patient.plan_text = ""
        self.assertIsNone(await self.handle("I lost my prescription"))
        self.assertEqual(self.sent, [])

    async def test_thursday_instead_moves_the_visit_and_confirms_it(self) -> None:
        result = await self.handle("ممكن أجي الخميس بدل الأربع؟")
        self.assertEqual(result["intent"], intents.RESCHEDULE_VISIT)
        # 2026-08-29 is a Saturday, so the next Thursday is five days out.
        self.assertEqual(self.visit.due_at, NOW + timedelta(days=5))
        self.assertEqual(len(self.queued), 1)
        said = self.to_patient()[0]
        # rev 17 item 10: the patient reads the day in words, in his own
        # language, not an ISO string. It is still the date the guard allowed.
        self.assertIn(timing.in_words(NOW + timedelta(days=5), "ar"), said)
        self.assertNotIn("2026-", said)
        self.assertEqual(self.cards(), [])

    async def test_another_day_with_no_day_in_it_stands_down(self) -> None:
        self.assertIsNone(await self.handle("can I come another day instead"))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.visit.due_at, NOW + timedelta(days=14))

    async def test_where_do_i_send_it_says_here_as_a_photo(self) -> None:
        from core import templates

        result = await self.handle("أبعتها فين؟")
        self.assertEqual(result["intent"], intents.WHERE_TO_SEND)
        self.assertEqual(self.to_patient(),
                         [templates.TEMPLATES["send_it_here"]["ar"]["m"]])
        self.assertEqual(self.queued, [])
        self.assertEqual(self.cards(), [])

    async def test_the_medicine_is_not_available_cards_the_doctor(self) -> None:
        result = await self.handle("الدوا مش متوفر في الصيدلية")
        self.assertEqual(result["intent"], intents.MEDICINE_UNAVAILABLE)
        self.assertEqual(self.medication.barrier, "availability")
        cards = self.cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["actions"][0]["label"], "Answer")
        self.assertTrue(any("No substitute" in line for line in cards[0]["lines"]))

    async def test_and_never_suggests_a_substitute(self) -> None:
        from core import templates

        await self.handle("الدوا مش متوفر في الصيدلية")
        said = self.to_patient()
        self.assertEqual(said, [templates.TEMPLATES["told_doctor"]["ar"]["m"]
                                .format(doctor="Test Doctor")])
        self.assertIn("مش هقترح بديل", said[0])

    async def test_an_english_message_is_answered_in_english(self) -> None:
        from core import templates

        await self.handle("the medicine is not available at the pharmacy")
        self.assertEqual(self.to_patient(),
                         [templates.TEMPLATES["told_doctor"]["en"]["m"]
                          .format(doctor="Test Doctor")])

    async def test_i_forgot_to_measure_records_the_gap_and_asks_again(self) -> None:
        result = await self.handle("نسيت أقيس الضغط النهاردة")
        self.assertEqual(result["intent"], intents.FORGOT_MEASURE)
        self.assertEqual(self.monitor.barrier, "forgot")
        self.assertIn("gap", result["detail"])
        self.assertEqual(len(self.queued), 1)
        self.assertIn(timing.in_words(NOW + timedelta(days=1), "ar"),
                      self.to_patient()[0])
        self.assertEqual(self.cards(), [])

    async def test_the_audit_line_names_the_tool_the_guard_and_the_intent(
            self) -> None:
        await self.handle("عملت التحليل امبارح")
        lines = [meta.get("audit", {}).get("line", "")
                 for _, _, meta in self.written]
        line = [one for one in lines if "administrative intent" in one]
        self.assertEqual(len(line), 1)
        self.assertIn("coordinator: schedule_next_contact accepted", line[0])
        self.assertIn("matched by code pattern", line[0])

    async def test_a_guard_that_refuses_stands_the_intent_down(self) -> None:
        """Six contacts is six contacts, whoever asked for the seventh."""
        self.test_loop.contacts = 6
        self.assertIsNone(await self.handle("عملت التحليل امبارح"))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queued, [])
        texts = [text for _, text, _ in self.written]
        self.assertTrue(any("stood down" in one for one in texts))

    async def test_an_unmatched_message_never_reaches_the_tools(self) -> None:
        with patch.object(intents, "model_vote", self.no_intent):
            self.assertIsNone(await self.handle("شكرا يا دكتور"))
        self.assertEqual(self.sent, [])

    @staticmethod
    async def no_intent(text):
        return ""

    async def test_the_model_vote_may_add_an_answer_only_match(self) -> None:
        """The vote still adds, and now only the two intents that change nothing."""
        async def voted(text):
            return intents.WHERE_TO_SEND

        with patch.object(intents, "model_vote", voted):
            result = await self.handle("طيب والنتيجه دي بقى")
        self.assertEqual(result["intent"], intents.WHERE_TO_SEND)
        self.assertTrue(result["answered"])

    async def test_the_model_vote_cannot_add_a_state_changing_intent(self) -> None:
        """codex re-audit 9. A vote naming did_test moved a loop's state.

        Four of the six intents change the plan of work, and a model naming one
        of those on a yes/no call is a model driving a state change. The vote
        is refused for all four now and the message falls through to the
        Coordinator, which is what happened before core/intents.py existed.
        """
        for intent in (intents.DID_TEST, intents.RESCHEDULE_VISIT,
                       intents.MEDICINE_UNAVAILABLE, intents.FORGOT_MEASURE):
            with self.subTest(intent=intent):
                async def voted(text, named=intent):
                    return named

                before = (self.test_loop.state, self.visit.due_at,
                          len(self.queued), len(self.sent))
                with patch.object(intents, "model_vote", voted):
                    result = await self.handle("خلاص الموضوع اتظبط والعينه اترفعت")
                self.assertIsNone(result)
                self.assertEqual(
                    (self.test_loop.state, self.visit.due_at,
                     len(self.queued), len(self.sent)), before)

    async def test_a_vote_that_fails_is_a_stand_down_and_not_a_guess(self) -> None:
        async def exploded(text):
            return ""  # what model_vote returns on any failure at all

        with patch.object(intents, "model_vote", exploded):
            self.assertIsNone(await self.handle("مش فاهم الموضوع"))
        self.assertEqual(self.sent, [])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheVoteFailsClosed(unittest.IsolatedAsyncioTestCase):
    async def test_any_failure_names_no_intent(self) -> None:
        from core import media

        class Broken:
            async def generate_content(self, **kw):
                raise RuntimeError("the model is down")

        with patch.object(media, "client",
                          type("C", (), {"aio": type("A", (), {
                              "models": Broken()})()})()):
            self.assertEqual(await intents.model_vote("عملت التحليل"), "")

    async def test_an_empty_message_is_never_voted_on(self) -> None:
        self.assertEqual(await intents.model_vote("   "), "")


if __name__ == "__main__":
    unittest.main()
