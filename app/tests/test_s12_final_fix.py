"""S12: the last defects, and the two-things-at-once half of them.

Two groups of test live here, and they are different in kind.

The first is what a doctor or a patient can hit with one tap: a deep link
opened in the doctor's own Telegram, a model outage during a dictation, an
identification note nobody validated, a relay promised before it existed, a
model vote driving a state change, a confirm card that hid a missing plan.
Every one of those was reproduced by hand before it was written down, so every
test here drives the real function and asserts on the record it left.

The second is two requests arriving at once. Those tests run the real code
twice under `asyncio.gather` against a store double whose transactional
functions hold an `asyncio.Lock`, which is the same all-or-nothing Firestore
gives a transaction. A double that let a read and a write be interleaved would
pass whatever the code did, so the lock is the point of the double and not an
implementation detail of it.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)

# The doctor's own Telegram chat, as a number with nothing behind it. It was
# Mohamed's real chat id until the 2026-08-30 deploy scan found it here: this
# file ships in the public repository, and a real person's chat id is not a
# test fixture. Nothing in these tests depends on the value.
DOCTOR_CHAT = 700100200

try:  # the cloud SDK is in the image; a laptop may not have it
    from core import (
        cards, chaser, concierge, coordinator, escalate, events as events_module,
        identify, intents, lang, links, registrar, settings, store as store_module,
        tasks, telegram, tg_router,
    )
    from core.models import (
        Doctor, Event, LinkToken, Loop, Patient, PendingConfirm, Relay, Send,
    )
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


class Recorder:
    """A fanout that keeps what was sent instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.fail_on = ""

    async def send(self, target_ref, msg):
        if self.fail_on and target_ref.startswith(self.fail_on):
            raise RuntimeError("the channel refused the message")
        self.sent.append((target_ref, msg))
        return f"receipt{len(self.sent)}"

    def to(self, prefix: str) -> list:
        return [m for ref, m in self.sent if ref.startswith(prefix)]


def a_doctor(**fields) -> Doctor:
    base = dict(id="d", name="Test Doctor", web_token="tok", created_at=NOW)
    base.update(fields)
    return Doctor(**base)


def a_patient(**fields) -> Patient:
    base = dict(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                created_at=NOW)
    base.update(fields)
    return Patient(**base)


def a_loop(**fields) -> Loop:
    base = dict(id="l", patient_id="p", doctor_id="d", type="TEST",
                title="Lipid panel", state="waiting_patient",
                due_at=NOW + timedelta(days=3), created_at=NOW, updated_at=NOW)
    base.update(fields)
    return Loop(**base)


# --------------------------------------------------------------------------- #
# Item 1: a doctor tapping his own patient's deep link
# --------------------------------------------------------------------------- #
class LinkStore:
    """Just enough store for /start: one doctor, one patient, one token."""

    def __init__(self) -> None:
        self.doctors: dict[str, Doctor] = {}
        self.patients: dict[str, Patient] = {}
        self.tokens: dict[str, LinkToken] = {}
        self.events: list[Event] = []
        self.pending: list = []
        self.counter = 0

    def now(self) -> datetime:
        return NOW

    def new_id(self) -> str:
        self.counter += 1
        return f"e{self.counter}"

    async def doctor_by_telegram(self, chat_id):
        for doctor in self.doctors.values():
            if doctor.telegram_chat_id == chat_id:
                return doctor
        return None

    async def doctor_by_id(self, doctor_id):
        return self.doctors.get(doctor_id)

    async def get_patient(self, patient_id):
        return self.patients.get(patient_id)

    async def patient_by_telegram(self, chat_id):
        for patient in self.patients.values():
            if (patient.channels or {}).get("telegram_chat_id") == chat_id:
                return patient
        return None

    async def update_patient(self, patient_id, **fields):
        patient = self.patients[patient_id]
        for key, value in fields.items():
            setattr(patient, key, value)

    async def get_link_token(self, token_id):
        return self.tokens.get(token_id)

    async def consume_link_token(self, token_id):
        token = self.tokens.get(token_id)
        if token is None or token.used or token.revoked:
            return None
        token.used = True
        return token

    async def add_event(self, event):
        self.events.append(event)
        return event

    async def list_events(self, doctor_id):
        return [e for e in self.events if e.doctor_id == doctor_id]

    async def save_pending_start(self, start):
        self.pending.append(start)
        return start


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ADoctorTappingAPatientLink(unittest.IsolatedAsyncioTestCase):
    """Mohamed's own bug, 2026-08-29 evening.

    He forwarded a patient link and tapped it himself to see what the patient
    would see. The bot bound his chat as that patient, burned the one-time
    token, sent him the patient welcome, and from then on every message he
    typed reached Sanad as that patient's words. The link he forwarded was
    already spent, so the real patient could never bind at all.
    """

    def setUp(self) -> None:
        self.fake = LinkStore()
        self.doctor = a_doctor(telegram_chat_id=DOCTOR_CHAT)
        self.patient = a_patient(channels={"web": True})
        self.fake.doctors["d"] = self.doctor
        self.fake.patients["p"] = self.patient
        self.fake.tokens["tk"] = LinkToken(id="tk", doctor_id="d",
                                           patient_id="p", created_at=NOW)
        self.said: list[tuple[int, str]] = []
        self.welcomed: list[str] = []
        self.out = Recorder()

        async def send_card(chat_id, text, card=None):
            self.said.append((chat_id, text))

        async def welcome(patient, doctor):
            self.welcomed.append(patient.id)

        self.enterContext(patch.object(tg_router, "fanout", lambda: self.out))

        for name in ("now", "new_id", "doctor_by_telegram", "doctor_by_id",
                     "get_patient", "patient_by_telegram", "update_patient",
                     "get_link_token", "consume_link_token", "add_event",
                     "list_events", "save_pending_start"):
            self.enterContext(patch.object(store_module, name,
                                           getattr(self.fake, name)))
        self.enterContext(patch.object(telegram, "send_card", send_card))
        self.enterContext(patch.object(links, "welcome", welcome))

    async def test_the_token_is_not_consumed_and_the_patient_is_not_bound(
            self) -> None:
        await tg_router._start(DOCTOR_CHAT, "tk", {"from": {"first_name": "Moh"}})

        self.assertFalse(self.fake.tokens["tk"].used)
        self.assertIsNone(
            (self.fake.patients["p"].channels or {}).get("telegram_chat_id"))
        self.assertEqual(self.welcomed, [])

    async def test_he_is_told_to_forward_it(self) -> None:
        await tg_router._start(DOCTOR_CHAT, "tk", {"from": {"first_name": "Moh"}})
        self.assertEqual(self.said, [(DOCTOR_CHAT, tg_router.DOCTOR_TAPPED_LINK)])

    async def test_his_own_board_records_that_the_link_is_still_valid(
            self) -> None:
        await tg_router._start(DOCTOR_CHAT, "tk", {"from": {"first_name": "Moh"}})
        [event] = [e for e in self.fake.events if e.doctor_id == "d"]
        self.assertIn("still valid", event.text)
        self.assertFalse(event.meta["consumed"])
        self.assertEqual(event.meta["token"], "tk")

    async def test_a_patient_tapping_the_same_link_still_binds(self) -> None:
        """The guard is about whose chat it is, and nothing else."""
        await tg_router._start(777, "tk", {"from": {"first_name": "Ahmed"}})

        self.assertTrue(self.fake.tokens["tk"].used)
        self.assertEqual(
            (self.fake.patients["p"].channels or {})["telegram_chat_id"], 777)
        self.assertEqual(self.welcomed, ["p"])


# --------------------------------------------------------------------------- #
# Item 2: finding a doctor's chat that is already bound as a patient
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheWrongBindingIsReported(unittest.IsolatedAsyncioTestCase):
    """The check half of item 1, for a board that already carries one.

    `core/tg_router._start` refuses the tap now, but the live board still had
    Mohamed's chat on a patient record and nothing anywhere said so. The repair
    is the one that already exists, `POST /admin/reset`, which deletes that
    board's patients; what was missing was anything that noticed. This is what
    notices, at startup and after every reset.
    """

    def setUp(self) -> None:
        self.fake = LinkStore()
        self.fake.doctors["d"] = a_doctor(telegram_chat_id=DOCTOR_CHAT)
        self.fake.doctors["d2"] = a_doctor(id="d2", name="Dr Two",
                                           web_token="tok2")
        self.fake.patients["p"] = a_patient(
            channels={"web": True, "telegram_chat_id": DOCTOR_CHAT})
        self.fake.patients["p2"] = a_patient(
            id="p2", name="Mona Said",
            channels={"web": True, "telegram_chat_id": 999})

        async def patients_by_telegram(chat_id):
            return [p for p in self.fake.patients.values()
                    if (p.channels or {}).get("telegram_chat_id") == chat_id]

        async def doctor_chat_bindings():
            """`store.doctor_chat_bindings`, with the doctor stream in memory."""
            out = []
            for doctor in self.fake.doctors.values():
                if doctor.telegram_chat_id is None:
                    continue
                for patient in await patients_by_telegram(
                        doctor.telegram_chat_id):
                    out.append({"patient_id": patient.id,
                                "patient_name": patient.name,
                                "doctor_id": doctor.id,
                                "doctor_name": doctor.name,
                                "chat_id": doctor.telegram_chat_id})
            return out

        self.enterContext(patch.object(store_module, "now", self.fake.now))
        self.enterContext(patch.object(store_module, "new_id",
                                       self.fake.new_id))
        self.enterContext(patch.object(store_module, "add_event",
                                       self.fake.add_event))
        self.enterContext(patch.object(store_module, "patients_by_telegram",
                                       patients_by_telegram))
        self.enterContext(patch.object(store_module, "doctor_chat_bindings",
                                       doctor_chat_bindings))

    async def test_only_a_doctors_own_chat_is_reported(self) -> None:
        rows = await tg_router.wrong_bindings()
        self.assertEqual([r["patient_id"] for r in rows], ["p"])
        self.assertEqual(rows[0]["chat_id"], DOCTOR_CHAT)
        self.assertEqual(rows[0]["doctor_id"], "d")

    async def test_the_doctor_reads_it_on_his_own_board(self) -> None:
        await tg_router.wrong_bindings()
        [event] = [e for e in self.fake.events if e.doctor_id == "d"]
        self.assertIn("your own Telegram chat is bound", event.text)
        self.assertIn("reset the board", event.text)
        self.assertEqual(event.patient_id, "p")

    async def test_a_clean_board_reports_nothing_and_writes_nothing(
            self) -> None:
        self.fake.patients["p"].channels = {"web": True}
        self.assertEqual(await tg_router.wrong_bindings(), [])
        self.assertEqual(self.fake.events, [])


# --------------------------------------------------------------------------- #
# Item 5: the ordinary relay is a record before it is a promise
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheOrdinaryRelayIsPersistedFirst(unittest.IsolatedAsyncioTestCase):
    """codex re-audit 3. The last relay path that spoke before it wrote.

    "I will ask your doctor and get back to you" is a promise about a record,
    and this branch made it and then tried to write the record. Every other
    escalating path was inverted at codex item 10 and this one was missed, so
    a Firestore timeout here left the worst state this system has: a patient
    who has stopped waiting, and a doctor who was never told.
    """

    def setUp(self) -> None:
        self.out = Recorder()
        self.doctor = a_doctor(name="Dr Mohamed")
        self.patient = a_patient(plan_text="Take one tablet at night.")
        self.events: list[tuple[str, str, dict]] = []
        self.relays: list = []
        self.fail_relay = False

        async def append_event(doctor_id, kind, text="", **kw):
            self.events.append((kind, text, kw.get("meta") or {}))
            return type("E", (), {"id": f"e{len(self.events)}"})()

        async def save_relay(relay):
            if self.fail_relay:
                raise RuntimeError("firestore is down")
            self.relays.append(relay)
            return relay

        async def nothing(*a, **kw):
            return None

        self.enterContext(patch.object(concierge, "fanout", lambda: self.out))
        self.enterContext(patch.object(escalate, "events",
                                       type("M", (), {"append_event": staticmethod(append_event)})))
        self.enterContext(patch.object(concierge.events, "append_event",
                                       append_event))
        self.enterContext(patch.object(store_module, "save_relay", save_relay))
        self.enterContext(patch.object(store_module, "now", lambda: NOW))
        self.enterContext(patch.object(store_module, "new_id", lambda: "r1"))
        self.enterContext(patch.object(concierge.chaser, "note_patient_reply",
                                       nothing))
        # S18 item 1 added a second store read on the way in: a reply revives
        # every unreachable loop. This case has no board behind it, so it is
        # stubbed exactly as the attempt reset above is. What is under test
        # here is the order of the relay writes, not the loops.
        self.enterContext(patch.object(concierge.chaser, "revive_unreachable",
                                       nothing))
        self.enterContext(patch.object(concierge, "record_reading", nothing))
        self.enterContext(patch.object(concierge.validator,
                                       "wants_treatment_change",
                                       lambda text: True))

    async def ask(self) -> None:
        await concierge.handle_patient_message(
            self.patient, self.doctor, "can I take two of these instead?",
            gate=concierge.sentinel.Sentinel())

    def texts(self, prefix: str) -> list[str]:
        return [m.text for ref, m in self.out.sent if ref.startswith(prefix)]

    async def test_the_doctor_card_is_written_before_the_patient_is_told(
            self) -> None:
        await self.ask()
        order = [ref.split(":")[0] for ref, _ in self.out.sent]
        self.assertEqual(order, ["doctor", "patient"])
        self.assertEqual(len(self.relays), 1)
        self.assertIn(concierge.relay_line(self.doctor, "x").split(" and ")[0],
                      self.texts("patient:")[0])

    async def test_a_relay_that_cannot_be_written_never_promises_anything(
            self) -> None:
        self.fail_relay = True
        await self.ask()

        said = self.texts("patient:")
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0], escalate.fail_closed_text("en", "m",
                                                            emergency=False))
        self.assertEqual(self.texts("doctor:"), [])
        self.assertEqual(self.relays, [])

    async def test_the_failure_is_on_the_board_and_not_only_in_a_log(
            self) -> None:
        self.fail_relay = True
        await self.ask()
        failed = [row for row in self.events
                  if row[2].get("error") == escalate.FAIL_CLOSED]
        self.assertEqual(len(failed), 1)

    async def test_the_turn_is_answered_and_never_a_five_hundred(self) -> None:
        """A relay that cannot be written is still a finished turn."""
        self.fail_relay = True
        await self.ask()  # no exception escapes


# --------------------------------------------------------------------------- #
# Item 7: a confirm card that says the plan is missing
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AMissingPlanIsPrintedAsMissing(unittest.TestCase):
    """rev 18 item 9. "Plan: " with nothing after it is a blank the eye skips.

    It is reached by one real gesture: a dictation about a patient already on
    the board needs no plan of its own, and "This is a new patient" then turns
    that proposal into a new record whose card had an empty plan line.
    """

    def record(self, plan: str):
        from core.models import ProposedRecord

        return ProposedRecord.model_validate({
            "patient": {"name": "Ahmed", "age": None, "sex": None,
                        "diagnosis": ""},
            "baseline": [], "targets": [], "plan_text": plan,
            "loops": [{"type": "TEST", "title": "Serum potassium",
                       "test_name": "Potassium", "due_in_days": 7}],
        })

    def test_no_plan_says_none_dictated(self) -> None:
        card = registrar.confirm_card(self.record(""), "c1", "Dr Mohamed")
        self.assertIn("Plan: none dictated", card["lines"])

    def test_whitespace_is_no_plan_either(self) -> None:
        card = registrar.confirm_card(self.record("   "), "c1", "Dr Mohamed")
        self.assertIn("Plan: none dictated", card["lines"])

    def test_a_dictated_plan_is_printed_as_it_always_was(self) -> None:
        card = registrar.confirm_card(self.record("Take one at night."), "c1",
                                      "Dr Mohamed")
        self.assertIn("Plan: Take one at night.", card["lines"])


# --------------------------------------------------------------------------- #
# Item 8: what a claim is worth five minutes later
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AClaimIsOnlyGoodForItsLease(unittest.TestCase):
    """codex re-audit 4. Every claim in core/store.py was permanent.

    An instance that died holding one left a row saying "somebody is doing
    this" for ever, and nobody ever was: the patient was never nudged again on
    that rung, and Confirm answered "already being made" about a record that
    was never made.
    """

    def test_a_fresh_claim_is_nobodys_to_take(self) -> None:
        fresh = {"claimed_at": NOW - timedelta(minutes=1)}
        self.assertFalse(store_module.claim_expired(fresh, NOW))

    def test_a_claim_past_the_lease_is_free(self) -> None:
        old = {"claimed_at": NOW - store_module.CLAIM_LEASE - timedelta(seconds=1)}
        self.assertTrue(store_module.claim_expired(old, NOW))

    def test_the_card_claims_iso_string_is_read_the_same_way(self) -> None:
        """A card claim lives in an event's meta map and is stored as text."""
        old = {"claimed_at": (NOW - timedelta(hours=2)).isoformat()}
        self.assertTrue(store_module.claim_expired(old, NOW))

    def test_a_row_that_records_no_moment_is_never_taken(self) -> None:
        """Fail closed: an unknown age might still be in flight."""
        self.assertFalse(store_module.claim_expired({}, NOW))

    def test_a_send_falls_back_to_when_it_was_created(self) -> None:
        """Every Send row carries created_at, which is when it was claimed."""
        old = {"created_at": NOW - timedelta(hours=1)}
        self.assertTrue(store_module.claim_expired(old, NOW))


# --------------------------------------------------------------------------- #
# Items 9, 10, 12, 13: two of everything, at the same time
# --------------------------------------------------------------------------- #
from tests import test_state_idempotency as state_tests  # noqa: E402


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TwoWakeUpsAtOnce(state_tests.ChaserHarness):
    """The kernel half of S12, driven with `asyncio.gather`.

    Every test here fires two real `chaser.fire` coroutines into one event loop
    and asserts on what the store holds afterwards. The store double's
    transactional functions have no await between their read and their write,
    which is exactly what a Firestore transaction buys, so a defect that needs
    two requests to show itself shows itself here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.second = state_tests.a_loop(id="l2", title="Blood pressure")
        self.db.loops[self.second.id] = self.second

    async def test_two_loops_of_one_patient_send_one_message(self) -> None:
        """codex re-audit 6. Both read "nobody has spoken to him today"."""
        results = await asyncio.gather(
            chaser.fire(self.payload(loop_id="l", force=False)),
            chaser.fire(self.payload(loop_id="l2", force=False)),
        )

        self.assertEqual(sum(1 for r in results if r.get("sent")), 1)
        self.assertEqual(len(self.to_patient()), 1)
        row = list(self.db.contacts.values())[0]
        self.assertEqual(row["count"], 1)

    async def test_the_loop_that_lost_spent_nothing_at_all(self) -> None:
        results = await asyncio.gather(
            chaser.fire(self.payload(loop_id="l", force=False)),
            chaser.fire(self.payload(loop_id="l2", force=False)),
        )
        won = "l" if results[0].get("sent") else "l2"
        lost = "l2" if won == "l" else "l"

        self.assertEqual(self.db.loops[lost].contacts, 0)
        self.assertEqual(self.db.loops[lost].attempts, 0)
        self.assertEqual(self.db.loops[won].contacts, 1)
        self.assertEqual(self.db.loops[won].attempts, 1)
        self.assertNotIn(f"{lost}:0:nudge:1", self.db.sends)
        # And the day belongs to the message that actually went out: one row,
        # counted once, naming the loop that spoke.
        [row] = list(self.db.contacts.values())
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["loops"], [won])

    async def test_the_refused_wake_up_comes_round_again(self) -> None:
        """It is deferred, never dropped: the queue holds tomorrow's copy."""
        await asyncio.gather(
            chaser.fire(self.payload(loop_id="l", force=False)),
            chaser.fire(self.payload(loop_id="l2", force=False)),
        )
        self.assertEqual(len(self.db.queued), 1)

    async def test_the_sixth_contact_is_sent_and_the_seventh_is_not(self) -> None:
        """The reservation spends the budget; it must not misread its own spend."""
        self.loop.contacts = 5
        first = await chaser.fire(self.payload(force=False))
        self.assertTrue(first["sent"], first.get("reason"))
        self.assertEqual(self.loop.contacts, 6)

        self.db.contacts.clear()          # a new day
        second = await chaser.fire(self.payload(attempt=2, force=False))
        self.assertFalse(second["sent"])
        self.assertEqual(self.loop.contacts, 6)

    async def test_a_reschedule_mid_flight_sends_nothing(self) -> None:
        """codex re-audit 9, with the move landing inside the model turn."""
        arrived, go = asyncio.Event(), asyncio.Event()

        async def slow_choice(turn):
            arrived.set()
            await go.wait()
            return None

        async def reschedule():
            await arrived.wait()
            await self.db.bump_schedule_version("l")
            go.set()

        with patch.object(coordinator, "_choose", slow_choice):
            result, _ = await asyncio.gather(
                chaser.fire(self.payload(force=False, schedule_version=0)),
                reschedule(),
            )

        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], chaser.SUPERSEDED)
        self.assertEqual(self.to_patient(), [])
        self.assertEqual(self.loop.attempts, 0)
        self.assertEqual(self.loop.contacts, 0)   # the reservation was refunded

    async def test_a_reply_mid_flight_sends_nothing_either(self) -> None:
        """The generation is checked in the same transaction as the version.

        A patient who answers resets the ladder (`bump_generation`), and the
        rung that was being decided when he answered is a rung about a question
        he has already answered.
        """
        arrived, go = asyncio.Event(), asyncio.Event()

        async def slow_choice(turn):
            arrived.set()
            await go.wait()
            return None

        async def replies():
            await arrived.wait()
            await self.db.bump_generation("l")
            go.set()

        with patch.object(coordinator, "_choose", slow_choice):
            result, _ = await asyncio.gather(
                chaser.fire(self.payload(force=False)),
                replies(),
            )

        self.assertFalse(result["sent"])
        self.assertEqual(self.to_patient(), [])

    async def test_a_silent_escalation_leaves_the_patient_contactable_today(
            self) -> None:
        """Fable's review of S12, R1. The day was spent on nothing.

        The reservation is written before the model turn, which is the race
        fix. When the turn ends by escalating to the doctor, nothing at all is
        said to the patient, and leaving the day spent refused his other
        loop's reminder that day with "one message per patient per day" when he
        had heard nothing.
        """
        async def escalates(turn):
            turn.propose("escalate_barrier", {"barrier": "cost"},
                         "he says the test is too expensive")
            return turn.decision

        relays: list = []

        async def save_relay(relay):
            relays.append(relay)
            return relay

        with patch.object(coordinator, "_choose", escalates), \
                patch.object(store_module, "save_relay", save_relay), \
                patch.object(store_module, "new_id", lambda: "r1"):
            first = await chaser.fire(self.payload(loop_id="l", force=False))

        self.assertEqual(len(relays), 1)   # the doctor was told

        self.assertFalse(first["sent"])
        self.assertEqual(self.to_patient(), [])
        self.assertEqual(self.db.contacts, {})       # the day is free again
        self.assertEqual(self.db.loops["l"].contacts, 0)

        second = await chaser.fire(self.payload(loop_id="l2", force=False))

        self.assertTrue(second["sent"], second.get("reason"))
        self.assertEqual(len(self.to_patient()), 1)

    async def test_a_superseded_delivery_gives_the_day_back_too(self) -> None:
        """Nothing reached the patient, so both budgets go back."""
        arrived, go = asyncio.Event(), asyncio.Event()

        async def slow_choice(turn):
            arrived.set()
            await go.wait()
            return None

        async def reschedule():
            await arrived.wait()
            await self.db.bump_schedule_version("l")
            go.set()

        with patch.object(coordinator, "_choose", slow_choice):
            result, _ = await asyncio.gather(
                chaser.fire(self.payload(loop_id="l", force=False,
                                         schedule_version=0)),
                reschedule(),
            )

        self.assertEqual(result["reason"], chaser.SUPERSEDED)
        self.assertEqual(self.db.contacts, {})
        second = await chaser.fire(self.payload(loop_id="l2", force=False))
        self.assertTrue(second["sent"], second.get("reason"))

    async def test_a_delivery_that_threw_keeps_the_day_spent(self) -> None:
        """The one path that does NOT hand it back, and it is deliberate.

        A message that was decided, counted and attempted may well have
        arrived. Counting a message that may not have landed is the smaller
        error; the other direction messages a patient twice.
        """
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload(loop_id="l", force=False))

        self.assertEqual(list(self.db.contacts.values())[0]["count"], 1)
        second = await chaser.fire(self.payload(loop_id="l2", force=False))
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], store_module.NO_DAY_LEFT)

    async def test_two_wake_ups_both_count_their_attempt(self) -> None:
        """codex re-audit 13. `attempts` was read from a snapshot and written back."""
        arrived = 0
        both = asyncio.Event()

        async def snapshot(loop_id):
            row = self.db.loops.get(loop_id)
            return row.model_copy(deep=True) if row else None

        async def wait_for_both(turn):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                both.set()
            await both.wait()
            return None

        with patch.object(store_module, "get_loop", snapshot), \
                patch.object(coordinator, "_choose", wait_for_both):
            await asyncio.gather(chaser.fire(self.payload(attempt=1)),
                                 chaser.fire(self.payload(attempt=2)))

        self.assertEqual(self.db.loops["l"].attempts, 2)

    async def test_two_evidence_requests_both_count(self) -> None:
        """The other counter of codex re-audit 13, the Coordinator's own."""
        await asyncio.gather(
            self.db.add_evidence_request("l"),
            self.db.add_evidence_request("l"),
        )
        self.assertEqual(self.db.loops["l"].evidence_requests, 2)


# --------------------------------------------------------------------------- #
# Item 12: one receipt per channel, across a retry in another process
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AFailedChannelIsTheOnlyOneRetried(unittest.IsolatedAsyncioTestCase):
    """codex re-audit 5, the half a fresh `Fanout` object cannot remember.

    `core/chaser.resend` runs in another request, so the object that delivered
    the first attempt is long gone. What survives is the send record, and the
    per-channel receipt lives on it.
    """

    def setUp(self) -> None:
        from core import adapters

        self.adapters = adapters
        self.rows: dict[str, dict] = {"s1": {}}
        self.web_events = 0
        outer = self

        class Web:
            name = "web"

            async def send(self, ref, msg):
                outer.web_events += 1
                return f"event{outer.web_events}"

        class TelegramDown:
            name = "telegram"

            async def send(self, ref, msg):
                raise RuntimeError("telegram refused it")

        async def channels_done(send_id):
            row = self.rows.get(send_id) or {}
            return frozenset(n for n in ("web", "telegram")
                             if row.get(f"{n}_done"))

        async def mark_channel_done(send_id, channel):
            self.rows.setdefault(send_id, {})[f"{channel}_done"] = True

        self.web, self.telegram = Web(), TelegramDown()
        self.enterContext(patch.object(adapters.store, "channels_done",
                                       channels_done))
        self.enterContext(patch.object(adapters.store, "mark_channel_done",
                                       mark_channel_done))

    def fanout(self):
        """A fresh fan-out, the way every request builds one."""
        out = self.adapters.Fanout()
        out.channels = (self.web, self.telegram)
        return out

    async def message(self):
        from core.adapters import OutboundMessage

        return OutboundMessage(text="a reminder", receipt="s1")

    async def test_two_failed_fan_outs_leave_exactly_one_web_event(self) -> None:
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.fanout().send("patient:p", await self.message())
        self.assertEqual(self.web_events, 1)

    async def test_the_receipt_records_which_channel_landed(self) -> None:
        with self.assertRaises(RuntimeError):
            await self.fanout().send("patient:p", await self.message())
        self.assertTrue(self.rows["s1"]["web_done"])
        self.assertNotIn("telegram_done", self.rows["s1"])

    async def test_a_message_with_no_receipt_is_unchanged(self) -> None:
        """Nothing else in Sanad carries one, and nothing else changes."""
        from core.adapters import OutboundMessage

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.fanout().send("patient:p",
                                         OutboundMessage(text="hello"))
        self.assertEqual(self.web_events, 2)
        self.assertEqual(self.rows["s1"], {})


# --------------------------------------------------------------------------- #
# Item 11: one question, answered once
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AClosedRelayIsNotAnsweredTwice(unittest.IsolatedAsyncioTestCase):
    """codex re-audit 17. The card claim stops two presses of one button.

    It does not stop the same question being answered from two surfaces: the
    console and the Telegram "Answer" flow both end here, and a card left open
    in a tab is a third way in. The relay's own state is what answers that.
    """

    def setUp(self) -> None:
        self.out = Recorder()
        self.doctor = a_doctor()
        self.patient = a_patient()
        self.relay = Relay(id="r1", doctor_id="d", patient_id="p",
                           question="can I stop it?", reason="change",
                           state="answered", created_at=NOW)

        async def get_relay(relay_id):
            return self.relay if relay_id == self.relay.id else None

        async def get_patient(patient_id):
            return self.patient

        self.enterContext(patch.object(concierge, "fanout", lambda: self.out))
        self.enterContext(patch.object(store_module, "get_relay", get_relay))
        self.enterContext(patch.object(store_module, "get_patient", get_patient))

    async def test_the_patient_is_not_written_to_a_second_time(self) -> None:
        await concierge.doctor_reply(self.doctor, "r1", "stop it for now")
        self.assertEqual(self.out.to("patient:"), [])

    async def test_the_doctor_is_told_which_of_his_taps_did_nothing(self) -> None:
        await concierge.doctor_reply(self.doctor, "r1", "stop it for now")
        [msg] = self.out.to("doctor:")
        self.assertEqual(msg.text, concierge.ALREADY_ANSWERED)

    async def test_an_open_relay_is_still_answered(self) -> None:
        self.relay = self.relay.model_copy(update={"state": "open"})
        with patch.object(store_module, "update_patient", self._nothing), \
                patch.object(store_module, "close_relay", self._nothing), \
                patch.object(store_module, "update_doctor", self._nothing), \
                patch.object(store_module, "now", lambda: NOW), \
                patch.object(concierge.events, "append_event", self._nothing), \
                patch.object(concierge.coordinator, "resume_after_answer",
                             self._nothing), \
                patch.object(concierge.lang, "for_patient", self._lang):
            await concierge.doctor_reply(self.doctor, "r1", "stop it for now")
        self.assertEqual(len(self.out.to("patient:")), 1)

    @staticmethod
    async def _nothing(*a, **kw):
        return None

    @staticmethod
    async def _lang(*a, **kw):
        return "en"


if __name__ == "__main__":
    unittest.main()
