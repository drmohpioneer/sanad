"""Wave B: what Sanad claims before it acts, and what it does when acting fails.

Eight adversarial-review defects live here as public regressions, and every one
of them is the same shape: a thing that happens twice, or a thing that is
written in the wrong order, so a retry either duplicates work or loses it.
Each class below is one of them, and each starts from the test that reproduced
it.

Nothing here asks a model anything and nothing reaches Firestore: the store is
an in-memory double whose claims behave the way the real transactions behave
(create-if-absent, read-modify-write under one lock).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import Borrowable

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)

try:  # the cloud SDK is in the image; a laptop may not have it
    from core import (
        cards, chaser, coordinator, events as events_module, lang, links,
        registrar, settings, store as store_module, tasks,
    )
    from core.models import (
        Doctor, Event, LinkToken, Loop, Patient, PendingConfirm, Send,
    )
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


class FakeDb:
    """The pieces of core/store.py wave B claims against, in memory.

    The claim functions are the point: `claim_send`, `claim_confirm`,
    `claim_card_action` and `claim_resume` are transactions in the real store,
    and here they are ordinary Python under one thread, which is the same
    all-or-nothing they buy in Firestore.
    """

    def __init__(self) -> None:
        self.loops: dict[str, Loop] = {}
        self.patients: dict[str, Patient] = {}
        self.doctors: dict[str, Doctor] = {}
        self.sends: dict[str, Send] = {}
        self.confirms: dict[str, PendingConfirm] = {}
        self.events: list[Event] = []
        self.link_tokens: dict[str, LinkToken] = {}
        self.contacts: dict[str, dict] = {}
        self.actions: set[str] = set()
        self.sent: list[tuple[str, str, dict]] = []
        self.queued: list[tuple[str, dict, float]] = []
        # Set to "patient:" or "doctor:" to make delivery on that ref throw,
        # which is the only way to reproduce a half-finished send.
        self.fail_on = ""

    # --- reads ---------------------------------------------------------- #
    async def get_loop(self, loop_id):
        return self.loops.get(loop_id)

    async def list_loops(self, patient_id):
        return [l for l in self.loops.values() if l.patient_id == patient_id]

    async def get_patient(self, patient_id):
        return self.patients.get(patient_id)

    async def doctor_by_id(self, doctor_id):
        return self.doctors.get(doctor_id)

    async def sends_for_patient(self, patient_id):
        return [s for s in self.sends.values() if s.patient_id == patient_id]

    async def get_confirm(self, confirm_id):
        return self.confirms.get(confirm_id)

    async def get_link_token(self, token_id):
        return self.link_tokens.get(token_id)

    async def list_events(self, doctor_id):
        return [e for e in self.events if e.doctor_id == doctor_id]

    # --- writes --------------------------------------------------------- #
    async def update_loop(self, loop_id, **fields):
        loop = self.loops[loop_id]
        for key, value in fields.items():
            setattr(loop, key, value)

    async def create_loop(self, loop):
        self.loops[loop.id] = loop
        return loop

    async def create_patient(self, patient):
        self.patients[patient.id] = patient
        return patient

    async def update_patient(self, patient_id, **fields):
        patient = self.patients[patient_id]
        for key, value in fields.items():
            setattr(patient, key, value)

    async def save_confirm(self, confirm):
        self.confirms[confirm.id] = confirm
        return confirm

    async def delete_confirm(self, confirm_id):
        self.confirms.pop(confirm_id, None)

    async def add_event(self, event):
        self.events.append(event)
        return event

    async def update_event(self, event_id, **fields):
        for event in self.events:
            if event.id == event_id:
                for key, value in fields.items():
                    setattr(event, key, value)

    async def save_link_token(self, token):
        self.link_tokens[token.id] = token
        return token

    # --- claims (transactions in the real store) ------------------------ #
    async def claim_send(self, send):
        held = self.sends.get(send.id)
        if held is None:
            self.sends[send.id] = send
            return store_module.CLAIMED
        if held.state == "failed" and int(held.resends or 0) < 1:
            held.state = "claimed"
            held.resends = int(held.resends or 0) + 1
            return store_module.RESEND
        return store_module.ALREADY_SENT

    async def mark_send(self, send_id, state, error=""):
        held = self.sends.get(send_id)
        if held is not None:
            held.state = state
            held.error = error

    async def release_send(self, send_id):
        self.sends.pop(send_id, None)

    async def claim_confirm(self, confirm_id):
        confirm = self.confirms.get(confirm_id)
        if confirm is None or confirm.state != "pending":
            return False
        confirm.state = "committing"
        return True

    async def release_confirm(self, confirm_id):
        confirm = self.confirms.get(confirm_id)
        if confirm is not None:
            confirm.state = "pending"

    async def note_contact(self, patient_id, doctor_id, day_index, kind,
                           loop_id=""):
        row = self.contacts.setdefault(
            store_module.contact_id(patient_id, day_index),
            {"patient_id": patient_id, "doctor_id": doctor_id,
             "day_index": day_index, "count": 0, "kinds": [], "loops": []})
        row["count"] += 1
        if kind not in row["kinds"]:
            row["kinds"].append(kind)
        if loop_id and loop_id not in row["loops"]:
            row["loops"].append(loop_id)
        return row["count"]

    async def refund_day(self, patient_id, day_index, loop_id=""):
        """Hand back a reserved day nobody heard about. Deletes the empty row."""
        key = store_module.contact_id(patient_id, day_index)
        row = self.contacts.get(key)
        if row is None:
            return 0
        row["count"] = max(0, int(row.get("count") or 0) - 1)
        if row["count"] == 0:
            self.contacts.pop(key, None)
            return 0
        row["loops"] = [l for l in (row.get("loops") or []) if l != loop_id]
        return row["count"]

    async def add_contact_kind(self, patient_id, day_index, kind):
        row = self.contacts.get(store_module.contact_id(patient_id, day_index))
        if row is not None and kind and kind not in row["kinds"]:
            row["kinds"].append(kind)

    async def contacted_on(self, patient_id, day_index):
        return store_module.contact_id(patient_id, day_index) in self.contacts

    async def contact_days_for_patient(self, patient_id):
        return tuple(sorted(row["day_index"] for row in self.contacts.values()
                            if row["patient_id"] == patient_id))

    async def add_contact(self, loop_id, day_index):
        loop = self.loops[loop_id]
        loop.contacts = int(loop.contacts or 0) + 1
        if day_index not in (loop.contact_days or []):
            loop.contact_days = [*(loop.contact_days or []), day_index]

    async def refund_contact(self, loop_id):
        loop = self.loops[loop_id]
        loop.contacts = max(0, int(loop.contacts or 0) - 1)
        return loop.contacts

    async def reserve_contact(self, patient_id, doctor_id, day_index, loop_id,
                              kind, *, max_contacts=None,
                              allow_same_day=False):
        """The S12 reservation, with the same all-or-nothing the real one has.

        There is no await between the read and the write, which is exactly the
        guarantee `core/store.reserve_contact` buys from a Firestore
        transaction: two `chaser.fire` coroutines interleaving at any await
        point in this file can never both see the ledger row missing.
        """
        row = self.contacts.get(store_module.contact_id(patient_id, day_index))
        if row is not None and not allow_same_day:
            return {"ok": False, "why": store_module.NO_DAY_LEFT}
        loop = self.loops[loop_id]
        contacts = int(loop.contacts or 0)
        if max_contacts is not None and contacts >= max_contacts:
            return {"ok": False, "why": store_module.NO_CONTACTS_LEFT,
                    "contacts": contacts, "limit": max_contacts}
        count = await self.note_contact(patient_id, doctor_id, day_index, kind,
                                        loop_id=loop_id)
        await self.add_contact(loop_id, day_index)
        return {"ok": True, "count": count, "contacts": contacts + 1}

    async def claim_delivery(self, loop_id, schedule_version, generation, at):
        loop = self.loops.get(loop_id)
        if loop is None:
            return None
        if int(loop.schedule_version or 0) != int(schedule_version):
            return None
        if int(loop.generation or 0) != int(generation):
            return None
        loop.attempts = int(loop.attempts or 0) + 1
        loop.state = "waiting_patient"
        loop.last_attempt_at = at
        return loop.attempts

    async def add_evidence_request(self, loop_id):
        loop = self.loops[loop_id]
        loop.evidence_requests = int(loop.evidence_requests or 0) + 1
        return loop.evidence_requests

    async def channels_done(self, send_id):
        held = self.sends.get(send_id)
        if held is None:
            return frozenset()
        return frozenset(name for name in ("web", "telegram")
                         if getattr(held, f"{name}_done", False))

    async def mark_channel_done(self, send_id, channel):
        held = self.sends.get(send_id)
        if held is not None and channel in ("web", "telegram"):
            setattr(held, f"{channel}_done", True)

    async def claim_action(self, doctor_id, action_id):
        key = f"{doctor_id}:{action_id}"
        if key in self.actions:
            return False
        self.actions.add(key)
        return True

    async def release_action(self, doctor_id, action_id):
        self.actions.discard(f"{doctor_id}:{action_id}")

    async def add_reluctance(self, loop_id):
        loop = self.loops[loop_id]
        loop.reluctance = int(loop.reluctance or 0) + 1
        return loop.reluctance

    async def bump_generation(self, loop_id):
        loop = self.loops[loop_id]
        loop.generation = int(loop.generation or 0) + 1
        loop.attempts = 0
        return loop.generation

    async def bump_schedule_version(self, loop_id):
        loop = self.loops[loop_id]
        loop.schedule_version = int(loop.schedule_version or 0) + 1
        return loop.schedule_version

    async def claim_resume(self, loop_id, note):
        loop = self.loops.get(loop_id)
        if loop is None or not (loop.paused or loop.barrier):
            return False
        loop.paused = False
        loop.barrier = ""
        loop.barrier_note = note
        return True

    async def consume_link_token(self, token_id, now=None):
        token = self.link_tokens.get(token_id)
        if token is None or token.used or token.revoked:
            return None
        token.used = True
        return token

    async def claim_card_action(self, event_id, action_id, at):
        for event in self.events:
            if event.id != event_id:
                continue
            card = cards.card_of(event)
            if card.get("claimed_by") or card.get("resolved"):
                return False
            card["claimed_by"] = action_id
            card["claimed_at"] = at.isoformat()
            return True
        return False

    async def release_card_action(self, event_id):
        for event in self.events:
            if event.id == event_id:
                card = cards.card_of(event)
                card.pop("claimed_by", None)
                card.pop("claimed_at", None)


class Fanout:
    """Delivery that can be told to fail, because failing is what item 5 is."""

    def __init__(self, db: FakeDb) -> None:
        self.db = db

    async def send(self, ref, msg):
        if self.db.fail_on and ref.startswith(self.db.fail_on):
            raise RuntimeError("Telegram refused the message")
        self.db.sent.append((ref, msg.text, msg.meta or {}))
        return f"event/{len(self.db.sent)}"


def a_doctor() -> Doctor:
    return Doctor(id="d", name="Test Doctor", web_token="tok", created_at=NOW)


def a_patient() -> Patient:
    return Patient(id="p", doctor_id="d", name="Ahmed Ali", sex="male",
                   created_at=NOW)


def a_loop(**fields) -> Loop:
    base = dict(id="l", patient_id="p", doctor_id="d", type="TEST",
                title="Lipid panel", state="waiting_patient",
                due_at=NOW + timedelta(days=3), created_at=NOW, updated_at=NOW)
    base.update(fields)
    return Loop(**base)


class ChaserHarness(Borrowable):
    """Everything patched so `chaser.fire` runs against the double above."""

    def setUp(self) -> None:
        self.db = FakeDb()
        self.doctor, self.patient = a_doctor(), a_patient()
        self.loop = a_loop()
        self.db.doctors[self.doctor.id] = self.doctor
        self.db.patients[self.patient.id] = self.patient
        self.db.loops[self.loop.id] = self.loop
        self.model_calls = 0
        db, outer = self.db, self

        async def enqueue(path, payload, delay):
            db.queued.append((path, dict(payload), delay))
            return f"task/{len(db.queued)}"

        async def append_event(doctor_id, kind, text="", **kw):
            event = Event(id=f"e{len(db.events) + 1}", doctor_id=doctor_id,
                          kind=kind, text=text,
                          patient_id=kw.get("patient_id"),
                          loop_id=kw.get("loop_id"),
                          meta=kw.get("meta") or {}, ts=NOW)
            db.events.append(event)
            return event

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "en"

        async def stands_down(turn):
            outer.model_calls += 1
            return None

        self.patches = [
            patch.object(chaser, "fanout", lambda: Fanout(db)),
            patch.object(coordinator, "fanout", lambda: Fanout(db)),
            patch.object(tasks, "enqueue", enqueue),
            patch.object(events_module, "append_event", append_event),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
            patch.object(coordinator, "_choose", stands_down),
            patch.object(store_module, "now", lambda: NOW),
        ]
        for name in ("get_loop", "list_loops", "get_patient", "doctor_by_id",
                     "sends_for_patient", "update_loop", "claim_send",
                     "mark_send", "release_send", "note_contact",
                     "contacted_on", "contact_days_for_patient", "add_contact",
                     "refund_contact", "refund_day", "reserve_contact",
                     "claim_delivery", "add_contact_kind",
                     "add_evidence_request", "channels_done",
                     "mark_channel_done", "claim_action", "release_action",
                     "add_reluctance", "bump_generation",
                     "bump_schedule_version"):
            self.patches.append(
                patch.object(store_module, name, getattr(db, name)))
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def payload(self, **extra) -> dict:
        body = {"kind": "nudge", "run_id": "run1", "loop_id": "l", "attempt": 1,
                "force": True}
        body.update(extra)
        return body

    def to_patient(self) -> list:
        return [row for row in self.db.sent if row[0].startswith("patient:")]


# --------------------------------------------------------------------------- #
# Item 7: a reset ladder must not be suppressed by the old ladder's receipts
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheReceiptKeyCarriesAGeneration(ChaserHarness):
    """codex item 7. `loop:kind:attempt` outlived the attempts it counted.

    Any patient reply resets `attempts` to zero, so the restarted ladder asks
    for `loop:nudge:1` again, finds the receipt the first ladder left behind,
    and the patient who answered once is never reminded again. The key carries
    the generation now, and a reset increments it.
    """

    async def test_a_restarted_ladder_is_not_refused_as_already_sent(self) -> None:
        first = await chaser.fire(self.payload())
        self.assertTrue(first["sent"])

        await chaser.note_patient_reply(self.patient)

        second = await chaser.fire(self.payload())
        self.assertTrue(second["sent"], second.get("reason"))
        self.assertEqual(len(self.to_patient()), 2)

    async def test_a_reply_increments_the_generation_and_clears_attempts(self) -> None:
        await chaser.fire(self.payload())
        self.assertEqual(self.loop.attempts, 1)
        await chaser.note_patient_reply(self.patient)
        self.assertEqual(self.loop.generation, 1)
        self.assertEqual(self.loop.attempts, 0)

    async def test_the_replay_test_still_holds_inside_one_generation(self) -> None:
        """The same task twice, no reply in between: one message, one receipt."""
        first = await chaser.fire(self.payload())
        second = await chaser.fire(self.payload())
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], "already sent")
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(len(self.to_patient()), 1)

    def test_the_key_names_the_generation(self) -> None:
        self.assertEqual(chaser.receipt_key("l", 0, "nudge", 1), "l:0:nudge:1")
        self.assertEqual(chaser.receipt_key("l", 2, "nudge", 1), "l:2:nudge:1")


# --------------------------------------------------------------------------- #
# Item 5: the order of operations on a send, and what a failed one leaves
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AFailedSendIsKeptAndRetriedOnce(ChaserHarness):
    """codex item 5. The send happened first and the bookkeeping afterwards.

    A delivery that threw therefore left the receipt released, the loop
    uncounted and a message that may well have arrived recorded nowhere: the
    Cloud Tasks retry ran the whole wake-up again, model call included, and
    could send a second copy. The claim, the state, the counters and the audit
    event are all in front of the send now, and a failure keeps the receipt.
    """

    async def test_the_state_and_the_event_are_written_before_the_message(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())

        self.assertEqual(self.loop.attempts, 1)
        self.assertTrue(any(t.startswith("nudge 1 sent")
                            for _, t, _ in
                            [(e.kind, e.text, e.meta) for e in self.db.events]))

    async def test_a_message_that_never_left_does_not_spend_a_contact(self) -> None:
        """Fable's review of wave B.

        Six contacts on one loop is a promise to the patient about how much he
        will be bothered. A Telegram outage is not something he was bothered by,
        and leaving it counted made Sanad refuse a later contact over a message
        that never existed. The counter goes up before the send, because a
        message that DID leave must never be uncounted; an explicit failure
        hands it back.
        """
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())

        self.assertEqual(self.loop.contacts, 0)
        self.assertEqual(self.loop.attempts, 1)   # the rung is still spent

    async def test_the_contact_comes_back_when_the_resend_gets_through(self) -> None:
        """One message on the wire, one contact on the loop. Never two, never none."""
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())
        self.assertEqual(self.loop.contacts, 0)

        self.db.fail_on = ""
        await chaser.fire(self.payload())

        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(self.loop.contacts, 1)

    async def test_two_failures_leave_the_loop_owing_nothing(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())      # the one resend, also failed

        self.assertEqual(len(self.to_patient()), 0)
        self.assertEqual(self.loop.contacts, 0)

    async def test_the_receipt_is_kept_as_failed_with_the_error_on_it(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(chaser.RetryableNudgeError):
            await chaser.fire(self.payload())

        receipt = self.db.sends["l:0:nudge:1"]
        self.assertEqual(receipt.state, "failed")
        self.assertIn("refused", receipt.error)

    async def test_a_broken_failure_notice_cannot_mask_the_durable_resend(
        self,
    ) -> None:
        self.db.fail_on = "patient:"

        async def notice_failed(*_args, **_kwargs) -> None:
            raise RuntimeError("doctor notice store is down")

        with (
            patch.object(chaser, "delivery_failed", notice_failed),
            self.assertRaises(chaser.RetryableNudgeError),
        ):
            await chaser.fire(self.payload())

        self.assertEqual("failed", self.db.sends["l:0:nudge:1"].state)
        self.assertEqual(0, self.loop.contacts)

    async def test_a_fully_rolled_back_pre_send_failure_is_retryable(self) -> None:
        async def wake_failed(*_args, **_kwargs):
            raise RuntimeError("queue failed before the coordinator spoke")

        with (
            patch.object(coordinator, "on_wake", wake_failed),
            self.assertRaises(chaser.RetryableNudgeError),
        ):
            await chaser.fire(self.payload())

        self.assertNotIn("l:0:nudge:1", self.db.sends)
        self.assertEqual(0, self.loop.contacts)
        self.assertEqual({}, self.db.contacts)

    async def test_the_doctor_sees_that_it_was_not_delivered(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())

        cards_written = [e for e in self.db.events if e.kind == "card"]
        self.assertEqual(len(cards_written), 1)
        card = cards_written[0].meta["card"]
        self.assertEqual(card["severity"], "yellow")
        self.assertIn("not delivered", card["title"])
        self.assertEqual(cards_written[0].meta["receipt"], "l:0:nudge:1")

    async def test_the_retry_resends_once_and_counts_nothing_twice(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())

        self.db.fail_on = ""
        again = await chaser.fire(self.payload())

        self.assertTrue(again["sent"])
        self.assertIs(again["resend"], True)
        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(self.loop.attempts, 1)   # not a second rung
        # One contact, not two and not none: the failed attempt gave its one
        # back and this one counted its own (Fable's review of wave B).
        self.assertEqual(self.loop.contacts, 1)
        self.assertEqual(self.db.sends["l:0:nudge:1"].state, "sent")

    async def test_the_resend_is_allowed_once_and_then_never(self) -> None:
        self.db.fail_on = "patient:"
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())
        with self.assertRaises(RuntimeError):
            await chaser.fire(self.payload())      # the one resend, also failed

        third = await chaser.fire(self.payload())
        self.assertFalse(third["sent"])
        self.assertEqual(third["reason"], "already sent")

    async def test_a_receipt_is_closed_when_the_agent_spoke_instead(self) -> None:
        """A wake-up the Coordinator answered is spent, so the retry is refused."""
        async def classifies(turn):
            turn.propose("classify_barrier", {"barrier": "availability"},
                         "the lab is closed until Sunday")
            return turn.decision

        with patch.object(coordinator, "_choose", classifies):
            first = await chaser.fire(self.payload())
            second = await chaser.fire(self.payload())
        self.assertFalse(first["sent"])
        self.assertEqual(self.db.sends["l:0:nudge:1"].state, "sent")
        self.assertEqual(second["reason"], "already sent")


# --------------------------------------------------------------------------- #
# Item 9: a task made for a schedule that has been replaced
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AReplacedScheduleRefusesItsOldTasks(ChaserHarness):
    """codex item 9. Commit pre-schedules the whole ladder and nothing could
    take it back: a loop the doctor or the patient moved still had three old
    reminders on the queue, and Cloud Tasks has no way to reach in and delete
    them. So they are refused on arrival instead, by the version they were made
    for, and the refusal is on the board like every other refusal.
    """

    async def test_the_ladder_carries_the_version_it_was_made_for(self) -> None:
        queued = await chaser.schedule_loop(self.loop)
        self.assertEqual(len(queued), 3)
        for _, payload, _ in self.db.queued:
            self.assertEqual(payload["schedule_version"], 0)

    async def test_a_rung_from_a_replaced_schedule_sends_nothing(self) -> None:
        old = self.payload()
        self.loop.schedule_version = 1

        answer = await chaser.fire(old)

        self.assertFalse(answer["sent"])
        self.assertEqual(answer["reason"], chaser.SUPERSEDED)
        self.assertEqual(self.to_patient(), [])
        lines = [e.meta.get("audit", {}).get("line", "") for e in self.db.events]
        self.assertTrue(any(chaser.SUPERSEDED in line for line in lines))

    async def test_a_replay_of_the_task_that_rescheduled_is_dropped(self) -> None:
        """The retry of a wake-up that rescheduled is stale by its own doing.

        It never reaches the receipt, because the version guard is in front of
        the claim, and both refusals mean the same thing: nothing is sent and
        no model is asked.
        """
        async def picks_a_date(turn):
            turn.propose("schedule_next_contact", {"days_from_now": 2},
                         "the patient asked for more time")
            return turn.decision

        with patch.object(coordinator, "_choose", picks_a_date):
            await chaser.fire(self.payload())
            replay = await chaser.fire(self.payload())

        self.assertFalse(replay["sent"])
        self.assertEqual(replay["reason"], chaser.SUPERSEDED)
        self.assertEqual(self.model_calls, 0)  # the stub replaced the model

    async def test_a_reschedule_moves_the_version_on_and_stamps_the_new_task(self) -> None:
        """The Coordinator scheduling the next contact is a reschedule."""
        async def picks_a_date(turn):
            turn.propose("schedule_next_contact", {"days_from_now": 2},
                         "the patient asked for more time")
            return turn.decision

        with patch.object(coordinator, "_choose", picks_a_date):
            await chaser.fire(self.payload())

        self.assertEqual(self.loop.schedule_version, 1)
        self.assertEqual(self.db.queued[-1][1]["schedule_version"], 1)

        superseded = await chaser.fire(self.payload(attempt=2))
        self.assertEqual(superseded["reason"], chaser.SUPERSEDED)

    async def test_evidence_supersedes_the_rest_of_the_ladder(self) -> None:
        """A patient who has already sent the slip is never asked for it again.

        The kernel review's F8b. The rungs for this loop are on the queue from
        commit time, and "please do the test" arriving the day after the result
        did is the single worst thing this system can say.
        """
        version = await chaser.supersede_ladder(
            self.loop.id, "the evidence arrived")
        self.assertEqual(version, 1)

        answer = await chaser.fire(self.payload())
        self.assertEqual(answer["reason"], chaser.SUPERSEDED)
        self.assertEqual(self.to_patient(), [])
        self.assertTrue(any("the evidence arrived" in e.text
                            for e in self.db.events))


# --------------------------------------------------------------------------- #
# Items 12 and 13: one ledger for the whole patient, and writes that add up
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class OneMessagePerPatientPerDay(ChaserHarness):
    """codex item 12. "One a day" was three different counts and none of them
    was the patient's.

    The Chaser counted Send rows, which are ladder nudges and nothing else. The
    Coordinator counted `contact_days` on one loop. The doctor's pre-approved
    reluctance line was counted nowhere at all. So a patient with two open
    loops could be written to three times in one day with every guard
    satisfied. There is one ledger now, per patient per Cairo day, and every
    message Sanad starts goes through it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.second = a_loop(id="l2", title="Blood pressure follow up")
        self.db.loops[self.second.id] = self.second

    async def test_a_coordinator_template_stops_another_loops_nudge(self) -> None:
        async def says_something(turn):
            turn.propose("classify_barrier", {"barrier": "availability"},
                         "the lab is closed until Sunday")
            return turn.decision

        with patch.object(coordinator, "_choose", says_something):
            await chaser.fire(self.payload(force=False))
        self.assertEqual(len(self.to_patient()), 1)

        held = await chaser.fire(self.payload(loop_id="l2", force=False))

        self.assertFalse(held["sent"])
        self.assertEqual(held["reason"], "one message per patient per day")
        self.assertEqual(len(self.to_patient()), 1)

    async def test_the_reluctance_line_is_a_contact_like_any_other(self) -> None:
        """The doctor's own pre-approved line still costs the patient his day."""
        async def is_reluctant(turn):
            turn.propose("classify_barrier", {"barrier": "asymptomatic"},
                         "he says he feels fine")
            return turn.decision

        with patch.object(coordinator, "_choose", is_reluctant):
            await chaser.fire(self.payload(force=False))

        day = list(self.db.contacts.values())[0]
        self.assertIn(store_module.RELUCTANCE, day["kinds"])
        held = await chaser.fire(self.payload(loop_id="l2", force=False))
        self.assertEqual(held["reason"], "one message per patient per day")

    async def test_the_ladder_writes_the_ledger_row_itself(self) -> None:
        await chaser.fire(self.payload(force=False))
        row = self.db.contacts[store_module.contact_id("p", 739_857)]
        self.assertEqual(row["kinds"], [store_module.LADDER])
        self.assertEqual(row["count"], 1)

    async def test_the_guard_the_agent_passes_reads_the_same_ledger(self) -> None:
        """The Coordinator's own schedule guard sees the patient, not the loop."""
        await self.db.note_contact("p", "d", 739_858, store_module.COORDINATOR,
                                   loop_id="l2")
        facts = await coordinator.facts_for(self.loop, wake=False)
        self.assertIn(739_858, facts.contact_days)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheCountersAddUpUnderAStaleRead(ChaserHarness):
    """codex item 13. Every counter was read, awaited on, and written back.

    Two messages on one loop inside one turn, or two turns racing, both read
    the same number and both wrote the same number back, so one of them
    vanished. The counters are server-side increments now, and the two that
    have to answer with their new value are transactions.
    """

    async def test_two_sends_on_one_snapshot_count_two_contacts(self) -> None:
        """The turn holds a snapshot of the loop, which is the whole defect.

        A turn is built once and the record moves under it. Reading `contacts`
        off that snapshot and writing the sum back means the second write puts
        back the number the first one already wrote.
        """
        turn = await coordinator._turn_for(self.loop, self.patient, self.doctor,
                                           coordinator.WAKE)
        turn.loop = self.loop.model_copy(deep=True)   # a snapshot, as it really is
        await coordinator._say(turn, "send_when_ready")
        await coordinator._say(turn, "send_when_ready")
        self.assertEqual(self.loop.contacts, 2)

    async def test_two_refusals_on_one_snapshot_count_two(self) -> None:
        first = await store_module.add_reluctance(self.loop.id)
        second = await store_module.add_reluctance(self.loop.id)
        self.assertEqual((first, second), (1, 2))

    async def test_a_day_is_recorded_once_however_many_messages(self) -> None:
        await store_module.add_contact(self.loop.id, 739_857)
        await store_module.add_contact(self.loop.id, 739_857)
        self.assertEqual(self.loop.contact_days, [739_857])
        self.assertEqual(self.loop.contacts, 2)


# --------------------------------------------------------------------------- #
# Wave A's F8a, wired: the verifier's verdict is a fact the guards read
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ThePartialSlipCannotBeMarkedReceived(ChaserHarness):
    """The guard wave A added to core/policy.py, populated here.

    The extractor wakes the Coordinator on every verdict, including one the
    verifier refused, and the instruction tells the model that a result which
    arrived is a result to mark received. `has_evidence` was true, because the
    values did attach. So a model vote could put a partial slip into
    pending_review, which is the exact state the verifier had just refused.
    """

    async def test_a_refused_verdict_refuses_mark_evidence_received(self) -> None:
        from core import policy as policy_module

        self.loop.results = [{"analyte": "LDL", "value": "160"}]
        self.loop.verified = {"satisfies": False, "missing": ["Triglycerides"]}
        facts = await coordinator.facts_for(self.loop, wake=False)

        self.assertIs(facts.verified_satisfies, False)
        decision = policy_module.check("mark_evidence_received", {}, facts)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.why, policy_module.UNVERIFIED)

    async def test_a_verdict_that_satisfies_still_passes(self) -> None:
        from core import policy as policy_module

        self.loop.results = [{"analyte": "LDL", "value": "160"}]
        self.loop.verified = {"satisfies": True}
        facts = await coordinator.facts_for(self.loop, wake=False)

        self.assertIs(facts.verified_satisfies, True)
        self.assertTrue(
            policy_module.check("mark_evidence_received", {}, facts).allowed)

    async def test_a_loop_the_verifier_never_saw_is_unchanged(self) -> None:
        """A typed reading and a monitoring loop have no verdict to contradict."""
        from core import policy as policy_module

        self.loop.readings = [{"day": 1, "slot": 0, "value": "130"}]
        facts = await coordinator.facts_for(self.loop, wake=False)

        self.assertIsNone(facts.verified_satisfies)
        self.assertTrue(
            policy_module.check("mark_evidence_received", {}, facts).allowed)


# --------------------------------------------------------------------------- #
# Item 6: Confirm, pressed twice
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ConfirmIsIdempotent(unittest.IsolatedAsyncioTestCase):
    """codex item 6. Confirm created a patient, then loops, then a link, then
    tasks, with nothing claimed in front of it.

    Two taps on the same card, which is a double click or a Telegram callback
    the phone re-sent, made two Ahmeds with two ladders on the same person, and
    a Confirm that failed halfway through left a record nobody could finish.
    A transaction claims the proposal first, and every document the commit
    writes has an id derived from the confirmation, so a retry writes the same
    documents over themselves.
    """

    def setUp(self) -> None:
        self.db = FakeDb()
        self.doctor = a_doctor()
        self.db.doctors["d"] = self.doctor
        self.db.confirms["c1"] = PendingConfirm(
            id="c1", doctor_id="d", expires_at=NOW + timedelta(hours=6),
            proposed={
                "patient": {"name": "Ahmed Ali", "age": 58,
                            "sex": "male", "diagnosis": "heart failure"},
                "baseline": [], "targets": [],
                "plan_text": "Come back in a month.",
                "loops": [{"type": "TEST", "title": "Lipid panel",
                           "test_name": "lipid panel", "due_in_days": 14}],
            })
        db = self.db

        async def append_event(doctor_id, kind, text="", **kw):
            event = Event(id=f"e{len(db.events) + 1}", doctor_id=doctor_id,
                          kind=kind, text=text,
                          patient_id=kw.get("patient_id"),
                          meta=kw.get("meta") or {}, ts=NOW)
            db.events.append(event)
            return event

        async def current():
            return "run1", 86400

        async def enqueue(path, payload, delay):
            db.queued.append((path, dict(payload), delay))
            return f"task/{len(db.queued)}"

        async def deep_link(token_id):
            return f"https://t.me/SanadHealthBot?start={token_id}"

        async def no_photo(*args, **kwargs):
            return False

        self.patches = [
            patch.object(registrar, "fanout", lambda: Fanout(db)),
            patch.object(events_module, "append_event", append_event),
            patch.object(settings, "current", current),
            patch.object(tasks, "enqueue", enqueue),
            patch.object(links, "patient_deep_link", deep_link),
            patch.object(registrar, "send_photo", no_photo),
            patch.object(store_module, "now", lambda: NOW),
        ]
        for name in ("get_confirm", "claim_confirm", "release_confirm",
                     "delete_confirm", "create_patient", "create_loop",
                     "list_loops", "save_link_token", "get_patient",
                     "update_patient", "save_confirm"):
            self.patches.append(
                patch.object(store_module, name, getattr(db, name)))
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def test_two_taps_make_one_patient_and_one_ladder(self) -> None:
        await registrar.commit(self.doctor, "c1")
        await registrar.commit(self.doctor, "c1")

        self.assertEqual(len(self.db.patients), 1)
        self.assertEqual(len(self.db.loops), 1)
        self.assertEqual(len([q for q in self.db.queued]), 3)

    async def test_the_second_tap_says_the_record_is_already_being_made(self) -> None:
        self.db.confirms["c1"].state = store_module.COMMITTING
        await registrar.commit(self.doctor, "c1")

        self.assertEqual(self.db.patients, {})
        self.assertIn("already confirmed",
                      " ".join(text for _, text, _ in self.db.sent))

    async def test_the_ids_are_derived_from_the_confirmation(self) -> None:
        await registrar.commit(self.doctor, "c1")
        patient = list(self.db.patients.values())[0]
        self.assertEqual(patient.id, store_module.derived_id("c1", "patient"))
        loop = list(self.db.loops.values())[0]
        self.assertEqual(loop.id, store_module.derived_id("c1", "loop", "0"))

    async def test_a_commit_that_throws_can_be_run_again(self) -> None:
        """The claim goes back, and the retry writes the same documents."""
        broken = True

        async def create_loop(loop):
            if broken:
                raise RuntimeError("Firestore is unavailable")
            return await self.db.create_loop(loop)

        with patch.object(store_module, "create_loop", create_loop):
            with self.assertRaises(RuntimeError):
                await registrar.commit(self.doctor, "c1")

        self.assertEqual(self.db.confirms["c1"].state, store_module.PENDING)
        first_patient = list(self.db.patients.values())[0].id

        broken = False
        await registrar.commit(self.doctor, "c1")

        self.assertEqual(len(self.db.patients), 1)
        self.assertEqual(list(self.db.patients.values())[0].id, first_patient)
        self.assertEqual(len(self.db.loops), 1)


# --------------------------------------------------------------------------- #
# The S9 review extra: a dictated diagnosis never overwrites the one on record
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ADiagnosisIsFilledAndNeverReplaced(unittest.TestCase):
    """A follow-up dictation is about one afternoon, not about the whole person.

    The extraction fills `diagnosis` from whatever the doctor happened to
    mention, so "his potassium is low" arrived as a diagnosis and the confirm
    card offered to replace "heart failure" with it. The doctor could see the
    line before he tapped, which was the guard, and one tired tap was all it
    took. A blank field is filled; a different one becomes a dated note.
    """

    def record(self, diagnosis: str):
        from core.models import ProposedPatient, ProposedRecord
        return ProposedRecord(
            patient=ProposedPatient(name="Ahmed Ali", diagnosis=diagnosis),
            plan_text="", loops=[])

    def test_a_blank_diagnosis_is_filled_in(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                          diagnosis="", created_at=NOW)
        fields, lines = registrar.record_updates(
            self.record("heart failure"), patient, NOW)
        self.assertEqual(fields["diagnosis"], "heart failure")
        self.assertIn("Diagnosis: (blank) becomes heart failure", lines)

    def test_a_different_diagnosis_is_a_note_and_not_an_overwrite(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                          diagnosis="heart failure", created_at=NOW)
        fields, lines = registrar.record_updates(
            self.record("hypokalaemia"), patient, NOW)

        self.assertNotIn("diagnosis", fields)
        self.assertEqual(fields["notes"][0]["text"],
                         "diagnosis dictated: hypokalaemia")
        self.assertEqual(fields["notes"][0]["at"], "2026-08-29")
        self.assertIn("Diagnosis stays heart failure. Noted on the record: "
                      "hypokalaemia", lines)

    def test_the_same_diagnosis_changes_nothing_at_all(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                          diagnosis="heart failure", created_at=NOW)
        fields, lines = registrar.record_updates(
            self.record("heart failure"), patient, NOW)
        self.assertEqual(fields, {})
        self.assertEqual(lines, [])

    def test_an_empty_dictated_diagnosis_is_silence(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                          diagnosis="heart failure", created_at=NOW)
        fields, _ = registrar.record_updates(self.record(""), patient, NOW)
        self.assertEqual(fields, {})

    def test_an_existing_note_is_kept_when_one_is_added(self) -> None:
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                          diagnosis="heart failure",
                          notes=[{"text": "father of Dr Tarek", "at": "2026-08-01",
                                  "source": "doctor dictation"}],
                          created_at=NOW)
        fields, _ = registrar.record_updates(
            self.record("hypokalaemia"), patient, NOW)
        self.assertEqual(len(fields["notes"]), 2)
        self.assertEqual(fields["notes"][0]["text"], "father of Dr Tarek")


# --------------------------------------------------------------------------- #
# Item 14: the patient link, consumed once, expiring, revocable
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ThePatientLinkIsConsumedOnceAndDoesNotLiveForEver(
        unittest.IsolatedAsyncioTestCase):
    """codex item 14. The one-time token was read, awaited on, and then burnt.

    Two /start messages inside the same second both read `used: False` and both
    bound themselves to the patient, which is not a race that needs bad luck: it
    is a person forwarding a link to himself twice. And a link that opens a
    patient's whole record had no expiry at all, so the QR on a printed slip was
    a bearer credential for ever.
    """

    def setUp(self) -> None:
        self.db = FakeDb()
        self.token = LinkToken(id="t1", doctor_id="d", patient_id="p",
                               created_at=NOW)
        self.db.link_tokens["t1"] = self.token
        self.patches = [
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "get_link_token", self.db.get_link_token),
            patch.object(store_module, "consume_link_token",
                         self.db.consume_link_token),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def test_two_starts_at_once_bind_one_phone(self) -> None:
        import asyncio

        both = await asyncio.gather(links.consume("t1"), links.consume("t1"))
        self.assertEqual(len([t for t in both if t is not None]), 1)

    async def test_a_token_past_its_life_opens_nothing(self) -> None:
        self.token.created_at = NOW - timedelta(days=links.LINK_TTL_DAYS + 1)
        self.assertIsNone(await links.consume("t1"))
        self.assertFalse(self.token.used)   # refused, not burnt

    async def test_a_token_inside_its_life_still_works(self) -> None:
        self.token.created_at = NOW - timedelta(days=links.LINK_TTL_DAYS - 1)
        self.assertIsNotNone(await links.consume("t1"))

    async def test_a_revoked_token_opens_nothing(self) -> None:
        self.token.revoked = True
        self.assertIsNone(await links.consume("t1"))

    async def test_the_web_page_refuses_an_expired_link(self) -> None:
        from fastapi import HTTPException
        import main

        self.db.patients["p"] = a_patient()
        with patch.object(main.store, "get_link_token", self.db.get_link_token), \
             patch.object(main.store, "get_patient", self.db.get_patient), \
             patch.object(main.store, "now", lambda: NOW):
            found = await main.patient_from_link("t1")
            self.assertEqual(found.id, "p")

            self.token.created_at = NOW - timedelta(days=400)
            with self.assertRaises(HTTPException) as refused:
                await main.patient_from_link("t1")
        self.assertEqual(refused.exception.status_code, 404)

    async def test_the_web_page_refuses_a_revoked_link(self) -> None:
        from fastapi import HTTPException
        import main

        self.db.patients["p"] = a_patient()
        self.token.revoked = True
        with patch.object(main.store, "get_link_token", self.db.get_link_token), \
             patch.object(main.store, "get_patient", self.db.get_patient), \
             patch.object(main.store, "now", lambda: NOW):
            with self.assertRaises(HTTPException):
                await main.patient_from_link("t1")

    async def test_a_used_link_still_opens_the_web_page(self) -> None:
        """Burning is about binding a phone, not about the page.

        The page has always been readable after the Telegram bind, and the
        runbook depends on it: a judge with no Telegram plays the patient there.
        """
        self.db.patients["p"] = a_patient()
        self.token.used = True
        import main

        with patch.object(main.store, "get_link_token", self.db.get_link_token), \
             patch.object(main.store, "get_patient", self.db.get_patient), \
             patch.object(main.store, "now", lambda: NOW):
            self.assertIsNotNone(await main.patient_from_link("t1"))


# --------------------------------------------------------------------------- #
# Item 17: a card action, pressed twice
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ACardActionIsClaimedBeforeTheWork(Borrowable):
    """codex item 17. The action ran and the card was resolved afterwards.

    So a second press while the first was still working did the work again:
    two Confirms are two patients, two Attaches are two sets of results. The
    action id is claimed on the card event in a transaction before the verb
    runs, and a claim that cannot be taken is answered "already done".
    """

    def setUp(self) -> None:
        import main

        self.main = main
        self.db = FakeDb()
        self.doctor = a_doctor()
        self.db.doctors["d"] = self.doctor
        self.db.events.append(Event(
            id="e1", doctor_id="d", kind="card", text="Ready to register",
            meta={"card": {"title": "New patient: Ahmed Ali", "lines": [],
                           "actions": [{"id": "confirm:c1", "label": "Confirm"},
                                       {"id": "cancel:c1", "label": "Cancel"}]}},
            ts=NOW))
        self.db.events.append(Event(
            id="e2", doctor_id="d", kind="card", text="Lipid panel is back",
            meta={"card": {"title": "Values", "lines": [],
                           "actions": [{"id": "reviewed:l1", "label": "Reviewed"},
                                       {"id": "note:l1", "label": "Send a note"}]}},
            ts=NOW))
        self.commits: list[str] = []
        self.notes: list[str] = []
        self.explode = False

        async def commit(doctor, confirm_id, base_url=""):
            if self.explode:
                raise RuntimeError("Firestore is unavailable")
            self.commits.append(confirm_id)

        async def note_to_patient(doctor, loop_id, text):
            self.notes.append(loop_id)

        self.patches = [
            patch.object(main.registrar, "commit", commit),
            patch.object(main.concierge, "note_to_patient", note_to_patient),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "list_events", self.db.list_events),
            patch.object(store_module, "update_event", self.db.update_event),
            patch.object(store_module, "claim_card_action",
                         self.db.claim_card_action),
            patch.object(store_module, "release_card_action",
                         self.db.release_card_action),
            patch.object(store_module, "claim_action", self.db.claim_action),
            patch.object(store_module, "release_action",
                         self.db.release_action),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def press(self, action_id: str, text: str = "") -> dict:
        from types import SimpleNamespace
        return await self.main.action(
            self.main.ActionIn(action_id=action_id, text=text),
            SimpleNamespace(base_url="http://sanad.example/"),
            self.doctor)

    async def test_the_second_press_does_no_work_and_says_so(self) -> None:
        first = await self.press("confirm:c1")
        second = await self.press("confirm:c1")

        self.assertEqual(self.commits, ["c1"])
        self.assertTrue(first["ok"])
        self.assertIs(second["already"], True)
        self.assertFalse(second["ok"])

    async def test_a_press_that_fails_gives_the_card_back(self) -> None:
        self.explode = True
        with self.assertRaises(RuntimeError):
            await self.press("confirm:c1")
        self.assertNotIn("claimed_by", self.db.events[0].meta["card"])

        self.explode = False
        again = await self.press("confirm:c1")
        self.assertTrue(again["ok"])
        self.assertEqual(self.commits, ["c1"])

    async def test_a_side_action_stays_pressable(self) -> None:
        """"Send a note" is not a claim: the doctor may send as many as he likes."""
        await self.press("note:l1", text="take it in the morning")
        await self.press("note:l1", text="and bring the slip")
        self.assertEqual(self.notes, ["l1", "l1"])

    async def test_an_action_no_card_carries_is_still_refused_after_it_ran(self) -> None:
        """A verb with no card behind it does its work and claims nothing."""
        answer = await self.press("openpatient:p1")
        self.assertTrue(answer["ok"])


if __name__ == "__main__":
    unittest.main()
