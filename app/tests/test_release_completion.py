"""Final regressions for the release-candidate issues found after audit three."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core import chaser, dispatch, extractor, identify, labs, registrar, sentinel, store, summary
from core.adapters import InboundMessage
from core.models import Doctor, Event, Loop, Patient, ProposedRecord
from tests.test_codex_races import _ClaimDb

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def proposed(**extra) -> ProposedRecord:
    body = {
        "patient": {"name": "Mona Fawzy", "age": 45, "sex": "female",
                    "diagnosis": "hypertension"},
        "plan_text": "Start amlodipine and come back in a month.",
        "loops": [{"type": "MEDICATION", "title": "Start amlodipine",
                   "drug": "amlodipine", "dose": "5", "action": "start"}],
        **extra,
    }
    return ProposedRecord.model_validate(body)


class MultiplePatientsAreNeverSilent(unittest.TestCase):
    SAID = ("Ahmed Ali needs a potassium in a week, and Mona Fawzy, 45, female, "
            "hypertension, start amlodipine 5 in the morning, come back in a month.")

    def test_source_backed_second_patient_is_on_every_confirmation_card(self) -> None:
        record = proposed(other_patients=[
            {"name": "Ahmed Ali", "instruction": "potassium in a week"}
        ])
        checked = record.model_copy(update={
            "other_patients": registrar.checked_other_patients(record, self.SAID)
        })
        card = registrar.confirm_card(checked, "c")
        joined = "\n".join(card["lines"])
        self.assertIn("🔴 SAFETY WARNING", joined)
        self.assertIn("Ahmed Ali", joined)
        self.assertIn("potassium in a week", joined)

    def test_existing_board_name_is_a_code_backstop_when_model_omits_it(self) -> None:
        record = proposed()
        board = [identify.BoardRow(id="p1", name="Ahmed Ali", age=58,
                                   diagnosis="heart failure")]
        checked = registrar.checked_other_patients(record, self.SAID, board)
        self.assertEqual([one.name for one in checked], ["Ahmed Ali"])

    def test_invented_name_and_instruction_are_never_displayed(self) -> None:
        record = proposed(other_patients=[
            {"name": "Khalid Ali", "instruction": "warfarin 10 mg"},
            {"name": "Ahmed Ali", "instruction": "warfarin 10 mg"},
        ])
        checked = registrar.checked_other_patients(record, self.SAID)
        self.assertEqual([(one.name, one.instruction) for one in checked],
                         [("Ahmed Ali", "")])

    def test_identity_choice_card_also_shows_the_warning(self) -> None:
        record = proposed(other_patients=[{"name": "Ahmed Ali"}])
        outcome = identify.Outcome(kind=identify.ASK, needs_name=True)
        card = registrar.ask_card(record, "c", [], outcome)
        self.assertIn("Ahmed Ali", "\n".join(card["lines"]))


class DurableClaims(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        class AlreadyExists(Exception):
            pass

        self.AlreadyExists = AlreadyExists
        self.db = _ClaimDb(AlreadyExists)
        self.stack = self.enterContext(
            patch.object(store, "db", return_value=self.db))
        self.enterContext(patch.object(store, "now", return_value=NOW))
        self.enterContext(patch.object(store.gexc, "AlreadyExists", AlreadyExists))
        self.enterContext(patch.object(store.firestore, "async_transactional",
                                       lambda fn: fn))

    async def test_two_patient_turn_claims_have_exactly_one_winner(self) -> None:
        won = await asyncio.gather(
            store.claim_patient_turn("p", "one"),
            store.claim_patient_turn("p", "two"),
        )
        self.assertEqual(sorted(won), [False, True])

    async def test_old_owner_cannot_release_a_reclaimed_patient_turn(self) -> None:
        await store.claim_patient_turn("p", "old")
        self.db.tables["patient_turns"]["p"]["claimed_at"] = (
            NOW - store.PATIENT_TURN_LEASE - timedelta(seconds=1))
        self.assertTrue(await store.claim_patient_turn("p", "new"))
        await store.release_patient_turn("p", "old")
        self.assertEqual(self.db.tables["patient_turns"]["p"]["claimed_by"], "new")

    async def test_identical_photo_claims_have_exactly_one_winner(self) -> None:
        won = await asyncio.gather(
            store.claim_photo("p", 1, "digest", "one"),
            store.claim_photo("p", 1, "digest", "two"),
        )
        self.assertEqual(sorted(won), [False, True])


class Recorder:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, target, message):
        self.sent.append((target, message))
        return str(len(self.sent))


class PatientInputBounds(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               created_at=NOW)
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="tok",
                             created_at=NOW)
        self.out = Recorder()
        self.held = {}
        self.ids = 0

        async def get_patient(_): return self.patient
        async def get_doctor(_): return self.doctor
        async def claim(pid, owner):
            if pid in self.held:
                return False
            self.held[pid] = owner
            return True
        async def release(pid, owner):
            if self.held.get(pid) == owner:
                self.held.pop(pid)
        def new_id():
            self.ids += 1
            return f"owner-{self.ids}"

        self.enterContext(patch.object(store, "get_patient", get_patient))
        self.enterContext(patch.object(store, "doctor_by_id", get_doctor))
        self.enterContext(patch.object(store, "claim_patient_turn", claim))
        self.enterContext(patch.object(store, "release_patient_turn", release))
        self.enterContext(patch.object(store, "new_id", new_id))
        self.enterContext(patch.object(dispatch, "fanout", return_value=self.out))
        self.enterContext(patch.object(sentinel, "check", AsyncMock(
            return_value=sentinel.Sentinel(checked=["code", "model"]))))

    def message(self, text: str) -> InboundMessage:
        return InboundMessage(channel="web", sender_ref="patient:p", text=text)

    async def test_1001_characters_cost_no_model_or_domain_turn(self) -> None:
        with patch.object(dispatch.concierge, "handle_patient_message",
                          AsyncMock()) as handle:
            await dispatch.handle_inbound(self.message("x" * 1001))
        handle.assert_not_awaited()
        sentinel.check.assert_not_awaited()
        self.assertIn("1,000", self.out.sent[0][1].text)

    async def test_exactly_1000_characters_are_accepted(self) -> None:
        with patch.object(dispatch.concierge, "handle_patient_message",
                          AsyncMock()) as handle:
            await dispatch.handle_inbound(self.message("x" * 1000))
        handle.assert_awaited_once()

    async def test_concurrent_second_turn_gets_busy_without_domain_work(self) -> None:
        entered, finish = asyncio.Event(), asyncio.Event()
        handled = []

        async def handle(_patient, _doctor, text, **_kwargs):
            handled.append(text)
            entered.set()
            await finish.wait()

        with patch.object(dispatch.concierge, "handle_patient_message", handle):
            first = asyncio.create_task(dispatch.handle_inbound(self.message("first")))
            await entered.wait()
            await dispatch.handle_inbound(self.message("second"))
            finish.set()
            await first
        self.assertEqual(handled, ["first"])
        self.assertTrue(any("previous message" in m.text for _, m in self.out.sent))

    async def test_code_emergency_bypasses_an_ordinary_turn_lock(self) -> None:
        entered, finish = asyncio.Event(), asyncio.Event()
        handled = []

        async def handle(_patient, _doctor, text, **kwargs):
            handled.append((text, kwargs["gate"].fired))
            if text == "ordinary question":
                entered.set()
                await finish.wait()

        with patch.object(dispatch.concierge, "handle_patient_message", handle):
            first = asyncio.create_task(
                dispatch.handle_inbound(self.message("ordinary question")))
            await entered.wait()
            await dispatch.handle_inbound(self.message("I have chest pain"))
            finish.set()
            await first
        self.assertIn(("I have chest pain", True), handled)


class PresentationAndCounting(unittest.TestCase):
    def test_arabic_reminders_do_not_insert_english_demo_test_names(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                          created_at=NOW)
        doctor = Doctor(id="d", name="Dr Mohamed", web_token="tok", created_at=NOW)
        loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                    title="Lipid panel", details={"test_name": "Lipid panel"},
                    created_at=NOW, updated_at=NOW)
        text = chaser.nudge_text(patient, doctor, loop, 1, "ar", "nudge")
        self.assertIn("تحليل الدهون", text)
        self.assertNotIn("Lipid panel", text)

    def test_identity_mismatch_card_has_no_order_contradiction_or_attach(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Wagdy Kamel", created_at=NOW)
        reading = extractor.PhotoReading(
            kind="lab_slip", text_orientation="upright",
            patient_name="Tarek Sobhy", taken_on="30/08/2026",
            analytes=[{"analyte": "Creatinine", "value": "1.0", "unit": "mg/dL"}],
        )
        findings = labs.assess([a.model_dump() for a in reading.analytes])
        card = extractor.identity_mismatch_card(
            patient, reading, findings, "gs://image", False, "e",
            "printed name Tarek Sobhy does not match Wagdy Kamel")
        joined = "\n".join(card["lines"])
        self.assertNotIn("Nothing was ordered", joined)
        self.assertNotIn("requested analytes", joined)
        self.assertEqual(card["actions"], [{"id": "seen:e", "label": "Seen"}])

    def test_duplicate_photo_events_are_counted_for_the_cairo_day(self) -> None:
        duplicate = Event(id="e", doctor_id="d", kind="system", ts=NOW,
                          meta={"duplicate_image": True})
        counts = summary.compute([], [duplicate], on=summary.today(NOW))
        self.assertEqual(counts.duplicates, 1)

    def test_browser_documents_include_focus_restoration_and_inline_favicons(self) -> None:
        dashboard = (ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
        patient = (ROOT / "web" / "patient.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="icon" href="data:,">', dashboard)
        self.assertIn('<link rel="icon" href="data:,">', patient)
        self.assertIn('setView("board", true)', dashboard)
        self.assertIn('el("patientBack").focus()', dashboard)
        self.assertIn('el("t").focus()', patient)


class MonitoringStartsAtConfirmation(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_is_day_one_and_seven_days_queue_six_reminders(self) -> None:
        loop = Loop(
            id="l", patient_id="p", doctor_id="d", type="MONITOR",
            title="Home blood pressure",
            details={"metric": "blood pressure", "days": 7},
            created_at=NOW, updated_at=NOW,
        )
        queued = []

        async def enqueue(path, payload, delay):
            queued.append((path, payload, delay))
            return f"task/{len(queued)}"

        with (
            patch.object(chaser.settings, "current", AsyncMock(
                return_value=("run", 3))),
            patch.object(chaser.tasks, "enqueue", enqueue),
        ):
            made = await chaser.schedule_loop(loop)

        self.assertEqual(len(made), 6)
        self.assertEqual([row[1]["attempt"] for row in queued],
                         [1, 2, 3, 4, 5, 6])
        self.assertEqual([row[2] for row in queued], [3, 6, 9, 12, 15, 18])

    async def test_the_beat_one_dictation_queues_twelve_follow_up_tasks(self) -> None:
        """S15 item 4. The number on the confirm feed line, measured.

        The runbook's beat 1 opens four obligations. Confirm writes
        "<n> follow-up tasks scheduled" with n taken from what was queued, so
        the string cannot go stale on its own, but the number a rehearsal is
        told to expect can, and it did: a seven-day monitor used to queue seven
        reminders and the whole dictation used to come to 13. Day one is now the
        confirmation itself, so it is 12, and that is asserted here rather than
        counted by hand off a screen.
        """
        started = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)

        def loop(kind, title, days=None, due=None):
            return Loop(id=title, patient_id="p", doctor_id="d", type=kind,
                        title=title,
                        due_at=started + timedelta(days=due) if due else None,
                        details={"metric": "blood pressure",
                                 "schedule": "twice a day", "days": days}
                        if days else {},
                        created_at=started, updated_at=started)

        # "Start atorvastatin 40 at night. Lipid panel in 2 weeks. Blood
        # pressure twice a day for 7 days. Come back in 3 weeks."
        beat_one = [
            loop("MEDICATION", "Start atorvastatin 40 at night"),
            loop("TEST", "Lipid panel", due=14),
            loop("MONITOR", "Blood pressure", days=7),
            loop("VISIT", "Follow-up visit", due=21),
        ]
        queued = []

        async def enqueue(path, payload, delay):
            queued.append(payload)
            return f"task/{len(queued)}"

        per_loop = []
        with (
            patch.object(chaser.settings, "current", AsyncMock(
                return_value=("run", 86400))),
            patch.object(chaser.tasks, "enqueue", enqueue),
            patch.object(chaser.store, "now", lambda: started),
        ):
            for one in beat_one:
                per_loop.append(len(await chaser.schedule_loop(one)))

        self.assertEqual(per_loop, [0, 3, 6, 3], "medication, lipid, BP, visit")
        self.assertEqual(len(queued), 12)
        self.assertEqual(sum(1 for p in queued if p["kind"] == "monitor"), 6)
        self.assertEqual(sum(1 for p in queued if p["kind"] == "nudge"), 6)


if __name__ == "__main__":
    unittest.main()
