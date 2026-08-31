"""Regression tests for the judge-visible and patient-safety round-two fixes.

The tests assert outcomes at public boundaries.  They do not inspect function
bodies or require Firestore, Telegram, a model, credentials, or a network.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core import (
    background, concierge, coordinator, events, identify, intents, labs,
    lang, policy, registrar, sentinel, store,
)
from core.models import Doctor, Loop, Patient


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
APP = Path(__file__).resolve().parents[1]


def doctor() -> Doctor:
    return Doctor(id="d", name="Dr Mohamed", web_token="token", created_at=NOW)


def patient(**changes) -> Patient:
    values = dict(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                  plan_text="Take bisoprolol 2.5 mg each morning.", created_at=NOW)
    values.update(changes)
    return Patient(**values)


def loop(loop_id: str = "l", **changes) -> Loop:
    values = dict(id=loop_id, patient_id="p", doctor_id="d", type="MONITOR",
                  title="Blood pressure", details={"metric": "blood pressure"},
                  state="waiting_patient", created_at=NOW, updated_at=NOW)
    values.update(changes)
    return Loop(**values)


class EnglishFirstSurface(unittest.IsolatedAsyncioTestCase):
    async def test_first_contact_is_english_until_the_patient_writes_arabic(self):
        p = patient()
        with patch.object(events, "last_events", AsyncMock(return_value=[])):
            self.assertEqual(await lang.for_patient(p, "d"), "en")
        history = [SimpleNamespace(patient_id="p", kind="patient_in",
                                   text="مساء الخير")]
        with patch.object(events, "last_events", AsyncMock(return_value=history)):
            self.assertEqual(await lang.for_patient(p, "d"), "ar")

    def test_patient_and_dashboard_static_controls_are_english(self):
        patient_html = (APP / "web" / "patient.html").read_text(encoding="utf-8")
        dashboard = (APP / "web" / "dashboard.html").read_text(encoding="utf-8")
        console = (APP / "web" / "console.html").read_text(encoding="utf-8")
        for phrase in ("Type here", "Send", "Lab photo or voice note",
                       "Reconnecting", "Not sent"):
            self.assertIn(phrase, patient_html)
        for phrase in ("Patients", "Inbox", "Reports", "Settings"):
            self.assertIn(phrase, dashboard)
        for surface in (dashboard, console):
            self.assertIn("What exactly is LDL?", surface)
            self.assertIn("I have chest pain", surface)
            self.assertIn("Follow-up reply", surface)
            self.assertNotIn("Chase now", surface)


class TheBackgroundBoardIsEnglish(unittest.TestCase):
    """S14 item 6, re-confirmed by S15 item 5 and turned into a rail here.

    The twenty invented patients are the board a judge lands on, so their names
    and everything written about them are English. The single exception the rule
    allows is a patient's own words: Arabic in a `said=` line is a patient who
    writes Arabic, which is the one thing that is supposed to reach Arabic. A
    confirmation that is only a paragraph in a results file drifts the next time
    somebody edits the table, so it is asserted instead.
    """

    ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

    def test_every_invented_name_is_written_in_latin_script(self) -> None:
        for person in background.PEOPLE:
            with self.subTest(name=person.name):
                self.assertIsNone(self.ARABIC.search(person.name))

    def test_what_the_doctor_reads_about_a_patient_is_english(self) -> None:
        for person in background.PEOPLE:
            for field in ("diagnosis", "plan"):
                with self.subTest(name=person.name, field=field):
                    self.assertIsNone(self.ARABIC.search(getattr(person, field)))

    def test_a_patients_own_words_are_arabic_only_if_he_speaks_arabic(self) -> None:
        for person in background.PEOPLE:
            if self.ARABIC.search(person.said or ""):
                with self.subTest(name=person.name):
                    self.assertEqual(person.speak, "ar")

    def test_the_fixture_carries_arabic_nowhere_but_a_patients_own_words(self) -> None:
        source = Path(background.__file__).read_text(encoding="utf-8")
        offenders = [line.strip() for line in source.splitlines()
                     if self.ARABIC.search(line) and "said=" not in line]
        self.assertEqual(offenders, [])


class LabWordingComesFromTheSlip(unittest.TestCase):
    def finding(self, value: str, ref_range: str, flag: str = ""):
        return labs.assess([{"analyte": "LDL", "value": value,
                             "unit": "mg/dL", "ref_range": ref_range,
                             "flag": flag}])[0]

    def test_a_flagged_ldl_outside_the_printed_range_is_not_called_in_range(self):
        result = self.finding("160", "< 100", "H")
        self.assertNotIn("in range", result.line.lower())
        self.assertIn("above the lab's reference", result.line)
        self.assertNotIn("critical", result.line.lower())

    def test_in_range_is_used_only_when_the_printed_range_contains_the_value(self):
        inside = self.finding("90", "70 - 100")
        outside = self.finding("160", "70 - 100")
        absent = self.finding("90", "")
        self.assertIn("in range", inside.line)
        self.assertNotIn("in range", outside.line)
        self.assertNotIn("in range", absent.line)

    def test_beta_hcg_spellings_reach_the_existing_pregnancy_rule(self):
        for spelling in ("Beta hCG (qualitative)", "B-hCG",
                         "Beta HCG, qualitative"):
            with self.subTest(spelling=spelling):
                self.assertEqual(labs.rule_for(spelling).analyte, "Pregnancy test")
                row = {"analyte": spelling, "value": "Positive", "unit": "",
                       "ref_range": "Negative", "flag": "Positive"}
                self.assertEqual(labs.assess([row], context=[])[0].level,
                                 "urgent_review")
                self.assertEqual(
                    labs.assess([row], context="severe abdominal pain")[0].level,
                    "critical",
                )


class PatientLanguageGates(unittest.TestCase):
    def test_laughing_idioms_do_not_wake_but_independent_emergencies_do(self):
        for text in ("هموت من الضحك", "hamoot mn el de7k", "I am dying laughing"):
            with self.subTest(text=text):
                self.assertIsNone(sentinel.code_net(text))
        for text in ("هموت من الضحك، مش قادر أتنفس",
                     "hamoot mn el de7k but I can't breathe",
                     "chest pain hahaha"):
            with self.subTest(text=text):
                self.assertIsNotNone(sentinel.code_net(text))

    def test_opt_out_is_explicit_and_negation_does_not_pause(self):
        for text in ("stop messaging me", "مش عايز تذكيرات",
                     "matb3atsh messages"):
            with self.subTest(text=text):
                self.assertTrue(intents.explicit_opt_out(text))
        self.assertFalse(intents.explicit_opt_out("do not stop messaging me"))

    def test_third_party_claims_are_narrowly_recognised(self):
        for text in ("I am his wife", "I am not Ahmed", "انا مش احمد"):
            with self.subTest(text=text):
                self.assertTrue(intents.third_party_identity(text))
        self.assertFalse(intents.third_party_identity(
            "My wife will collect the prescription"))

    def test_placeholder_names_are_never_patient_names(self):
        for name in ("unspecified", "not specified", "غير محدد"):
            with self.subTest(name=name):
                self.assertTrue(registrar.is_placeholder_name(name))

    def test_a_bare_name_is_a_lookup_not_a_plan(self):
        self.assertTrue(identify.is_bare_name("Ahmed Ali", "Ahmed Ali"))
        self.assertFalse(identify.is_bare_name(
            "Ahmed Ali needs a lipid panel", "Ahmed Ali"))


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail_patient_once = False

    async def send(self, target, message):
        if self.fail_patient_once and target.startswith("patient:"):
            self.fail_patient_once = False
            raise RuntimeError("patient channel down")
        self.sent.append((target, message))
        return f"event-{len(self.sent)}"


class ConciergeCodeGateHarness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.doctor = doctor()
        self.patient = patient()
        self.loops: list[Loop] = []
        self.readings: list[tuple[str, dict]] = []
        self.relays: list = []
        self.out = Recorder()
        self.receipts: dict[str, str] = {}
        self.opt_out_claims = 0
        self.change_vote = AsyncMock(return_value=False)
        self.answer_model = AsyncMock(
            side_effect=AssertionError("generative answer was called"))
        outer = self

        async def append_event(*args, **kwargs):
            return SimpleNamespace(id=f"event-{len(outer.out.sent) + 1}")

        async def note_patient_reply(*args, **kwargs):
            return None

        async def patient_language(*args, **kwargs):
            return "en"

        async def list_loops(_patient_id):
            return outer.loops

        async def update_loop(loop_id, **fields):
            found = next(item for item in outer.loops if item.id == loop_id)
            for key, value in fields.items():
                setattr(found, key, value)

        async def append_reading(loop_id, row):
            outer.readings.append((loop_id, row))

        async def claim_opt_out(_patient_id):
            outer.opt_out_claims += 1
            if outer.patient.proactive_paused:
                return False
            outer.patient.proactive_paused = True
            outer.patient.opt_out_at = NOW
            return True

        async def save_relay(relay):
            outer.relays.append(relay)
            return relay

        async def claim_send(send):
            state = outer.receipts.get(send.id)
            if state is None:
                outer.receipts[send.id] = store.CLAIMED
                return store.CLAIMED
            if state == "failed":
                outer.receipts[send.id] = store.CLAIMED
                return store.RESEND
            return store.ALREADY_SENT

        async def mark_send(send_id, state, error=""):
            outer.receipts[send_id] = state

        async def send_state(send_id):
            return outer.receipts.get(send_id, "")

        self.patches = [
            patch.object(concierge, "fanout", lambda: self.out),
            patch.object(concierge.events, "append_event", append_event),
            patch.object(concierge.chaser, "note_patient_reply", note_patient_reply),
            patch.object(concierge.lang, "for_patient", patient_language),
            patch.object(store, "list_loops", list_loops),
            patch.object(store, "update_loop", update_loop),
            patch.object(store, "append_reading", append_reading),
            patch.object(store, "claim_opt_out", claim_opt_out),
            patch.object(store, "save_relay", save_relay),
            patch.object(store, "claim_send", claim_send),
            patch.object(store, "mark_send", mark_send),
            patch.object(store, "send_state", send_state),
            patch.object(store, "now", lambda: NOW),
            patch.object(store, "new_id", lambda: f"relay-{len(self.relays) + 1}"),
            patch.object(concierge.validator, "model_change_vote",
                         self.change_vote),
            patch.object(concierge, "answer", self.answer_model),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self.patches])

    async def handle(self, text: str, gate=None):
        await concierge.handle_patient_message(
            self.patient, self.doctor, text,
            gate=gate if gate is not None else sentinel.Sentinel(),
        )

    def to(self, prefix: str):
        return [message for target, message in self.out.sent
                if target.startswith(prefix)]

    async def test_a_normal_bare_bp_is_filed_without_a_model_or_doctor_card(self):
        self.loops = [loop()]
        await self.handle("120/80")
        self.assertEqual(len(self.readings), 1)
        self.assertEqual(self.readings[0][0], "l")
        self.assertIn("Recorded 120/80", self.to("patient:")[0].text)
        self.assertEqual(self.to("doctor:"), [])
        self.change_vote.assert_not_awaited()
        self.answer_model.assert_not_awaited()

    # ---- S24 finding 6: a pressure written inside a sentence -------------
    # Both messages below were sent to the live service on rev 31. The first
    # filed nothing, so the Closure Auditor later reported all fourteen
    # readings missing about a patient who had just reported two. The second
    # escalated on a model triage vote, which is the exact card the comment in
    # core/concierge.py says was found and fixed - it had been fixed for a bare
    # "190/125" only.
    async def test_two_pressures_in_one_sentence_are_both_filed(self):
        self.loops = [loop()]
        with patch.object(concierge.intents, "handle",
                          AsyncMock(return_value=None)), \
             patch.object(concierge.coordinator, "carrying",
                          lambda loops, text: loops[0]), \
             patch.object(concierge.coordinator, "on_patient_reply",
                          AsyncMock(return_value="answered")):
            await self.handle("Morning BP 148/92, evening 152/95")

        self.assertEqual([row["value"] for _, row in self.readings],
                         ["148/92", "152/95"])
        self.assertEqual([row["number"] for _, row in self.readings],
                         [148.0, 152.0])
        # The sentence is still answered as a sentence: the Coordinator carried
        # it, exactly as it did live, and no fixed reading template replaced
        # the reply.
        self.assertEqual(self.to("patient:"), [])
        self.answer_model.assert_not_awaited()

    async def test_a_crisis_inside_a_sentence_escalates_in_code(self):
        self.loops = [loop()]
        escalated = AsyncMock()
        with patch.object(concierge.extractor, "escalate_bp", escalated):
            await self.handle(
                "My BP this morning was 190/125 and I have a bad headache")

        self.assertEqual([row["value"] for _, row in self.readings], ["190/125"])
        escalated.assert_awaited_once()
        verdict = escalated.await_args.args[2]
        self.assertEqual("crisis", verdict.level)
        self.assertEqual((190, 125), (verdict.systolic, verdict.diastolic))
        # The card names three numbers in code, not a vote.
        self.assertEqual("code", verdict.as_meta()["net"])
        self.assertIn("core/vitals.py", verdict.as_meta()["decided_by"])
        # The table ran before any model was asked, which is the whole point.
        self.change_vote.assert_not_awaited()
        self.answer_model.assert_not_awaited()

    async def test_the_worst_pressure_in_a_sentence_is_the_one_escalated(self):
        """Two readings, one of them a crisis. The crisis is not the first."""
        self.loops = [loop()]
        escalated = AsyncMock()
        with patch.object(concierge.extractor, "escalate_bp", escalated):
            await self.handle("Yesterday 140/85, this morning 195/130")

        self.assertEqual([row["value"] for _, row in self.readings],
                         ["140/85", "195/130"])
        self.assertEqual((195, 130), (escalated.await_args.args[2].systolic,
                                      escalated.await_args.args[2].diastolic))

    async def test_a_date_in_a_sentence_is_not_filed_as_a_pressure(self):
        """The guard that makes the search safe, driven rather than described."""
        self.loops = [loop()]
        with patch.object(concierge.intents, "handle",
                          AsyncMock(return_value=None)), \
             patch.object(concierge.coordinator, "carrying",
                          lambda loops, text: loops[0]), \
             patch.object(concierge.coordinator, "on_patient_reply",
                          AsyncMock(return_value="answered")):
            await self.handle("I did the test on 28/08/2026 and took 1/2 tablet")

        self.assertEqual(self.readings, [])

    async def test_a_normal_bp_without_a_monitor_is_not_claimed_as_recorded(self):
        await self.handle("120/80")
        self.assertEqual(self.readings, [])
        self.assertNotIn("Recorded", self.to("patient:")[0].text)
        self.assertIn("no open monitoring request", self.to("patient:")[0].text)

    async def test_a_red_bare_bp_keeps_the_critical_code_path(self):
        self.loops = [loop()]
        escalated = AsyncMock()
        with patch.object(concierge.extractor, "escalate_bp", escalated):
            await self.handle("185/125")
        self.assertEqual(len(self.readings), 1)
        escalated.assert_awaited_once()
        self.change_vote.assert_not_awaited()
        self.answer_model.assert_not_awaited()

    async def test_bp_with_prose_still_reaches_the_sentinel(self):
        self.loops = [loop()]
        await self.handle(
            "120/80 and I have chest pain",
            sentinel.Sentinel(fired=True, net="code",
                              concept="chest pain / pressure"),
        )
        # S24: the reading is now filed on the way past. It used to be dropped
        # for being written in a sentence, which is how a patient who had just
        # reported two pressures was told he had missed all fourteen. The
        # sentence itself still reaches the Sentinel, which is what this test
        # is here for, and the chest pain still ends the turn as an emergency.
        self.assertEqual([row["value"] for _, row in self.readings], ["120/80"])
        self.assertIn("emergency", self.to("patient:")[0].text.lower())
        self.change_vote.assert_not_awaited()
        self.answer_model.assert_not_awaited()

    async def test_opt_out_pauses_only_live_loops_and_notifies_each_side_once(self):
        self.loops = [loop("open", state="open"),
                      loop("waiting", state="waiting_patient"),
                      loop("done", state="done")]
        await self.handle("stop messaging me")
        await self.handle("stop messaging me")
        self.assertTrue(self.patient.proactive_paused)
        self.assertTrue(self.loops[0].paused)
        self.assertTrue(self.loops[1].paused)
        self.assertFalse(self.loops[2].paused)
        self.assertEqual(len(self.to("doctor:")), 1)
        self.assertEqual(len(self.to("patient:")), 1)

    async def test_an_opt_out_partial_delivery_retries_without_duplicate_card(self):
        self.loops = [loop()]
        self.out.fail_patient_once = True
        with self.assertRaises(RuntimeError):
            await self.handle("stop messaging me")
        await self.handle("stop messaging me")
        self.assertEqual(len(self.to("doctor:")), 1)
        self.assertEqual(len(self.to("patient:")), 1)

    async def test_an_inflight_doctor_card_never_allows_the_patient_promise(self):
        self.loops = [loop()]
        doctor_receipt = store.derived_id("opt-out", self.patient.id, "doctor")
        self.receipts[doctor_receipt] = store.CLAIMED
        await self.handle("stop messaging me")
        self.assertEqual(self.to("doctor:"), [])
        self.assertEqual(self.to("patient:"), [])

        # A later retry that owns the failed receipt completes the card first,
        # and only then makes the acknowledgement promise.
        self.receipts[doctor_receipt] = "failed"
        await self.handle("stop messaging me")
        self.assertEqual(len(self.to("doctor:")), 1)
        self.assertEqual(len(self.to("patient:")), 1)

    async def test_emergency_handling_still_precedes_opt_out(self):
        await self.handle(
            "stop messaging me, I can't breathe",
            sentinel.Sentinel(fired=True, net="code", concept="dyspnea at rest"),
        )
        self.assertFalse(self.patient.proactive_paused)
        self.assertEqual(self.opt_out_claims, 0)
        self.assertIn("emergency", self.to("patient:")[0].text.lower())

    async def test_a_third_party_gets_a_relay_not_the_plan_or_dose(self):
        await self.handle("I am his wife. What is his 2.5 mg dose?")
        self.assertEqual(len(self.relays), 1)
        [reply] = self.to("patient:")
        self.assertNotIn("2.5", reply.text)
        self.assertNotIn("bisoprolol", reply.text.lower())
        self.assertEqual(len(self.to("doctor:")), 1)


class VerifierEvidenceAndReminderTemplates(unittest.IsolatedAsyncioTestCase):
    async def test_the_patient_is_asked_for_every_analyte_the_verifier_says_missing(self):
        d, p = doctor(), patient()
        test_loop = loop(type="TEST", title="Lipid panel",
                         verified={"missing": ["Triglycerides", "HDL"]})
        turn = coordinator.Turn(
            doctor=d, patient=p, loop=test_loop, trigger=coordinator.EVIDENCE,
            facts=policy.LoopFacts(now=NOW), policy=policy.DEFAULT, speak="en",
        )
        decision = policy.Decision(
            tool="request_missing_evidence", allowed=True,
            args={"analyte": "LDL"}, reason="one model-selected part",
        )
        said = AsyncMock()
        with (patch.object(coordinator, "_say", said),
              patch.object(store, "add_evidence_request", AsyncMock(return_value=1)),
              patch.object(coordinator.events, "append_event", AsyncMock())):
            await coordinator._execute(turn, decision)
        fields = said.await_args.kwargs
        self.assertIn("Triglycerides", fields["analyte"])
        self.assertIn("HDL", fields["analyte"])
        self.assertNotIn("LDL", fields["analyte"])

    def test_visit_reminders_talk_about_the_visit_not_a_photo(self):
        visit = loop(type="VISIT", title="Follow-up visit")
        text = __import__("core.chaser", fromlist=["nudge_text"]).nudge_text(
            patient(), doctor(), visit, 1, "en", "nudge")
        self.assertIn("appointment", text.lower())
        self.assertNotIn("photo", text.lower())

    async def test_a_doctor_reply_never_resumes_or_schedules_a_closed_loop(self):
        closed = loop(type="VISIT", state="done", title="Follow-up visit")
        recorded = AsyncMock(return_value=SimpleNamespace(id="event"))
        claim = AsyncMock(return_value=True)
        enqueue = AsyncMock()
        with (patch.object(store, "get_loop", AsyncMock(return_value=closed)),
              patch.object(store, "claim_resume", claim),
              patch.object(coordinator.events, "append_event", recorded),
              patch.object(coordinator.tasks, "enqueue", enqueue)):
            result = await coordinator.resume_after_answer(
                doctor(), SimpleNamespace(loop_id=closed.id), "please resume")
        self.assertFalse(result["resumed"])
        self.assertFalse(result["scheduled"])
        claim.assert_not_awaited()
        enqueue.assert_not_awaited()
        self.assertIn("loop is done", recorded.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
