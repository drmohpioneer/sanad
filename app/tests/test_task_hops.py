"""The 720 hour ceiling, and the four things that broke because of it.

Cloud Tasks refuses a scheduleTime more than 720 hours out. Live, on rev
sanad-00015-p6x, that turned the demo's own first sentence ("come back in a
month") into a 500 at Confirm with no patient link ever minted: the loops were
written, the enqueue threw, and `links.mint` never ran.

Four things are proved here:

  the clamp      no task is ever created with a delay past MAX_DELAY_SECONDS,
                 and one that had to be clamped remembers its real moment;
  the hop        a task that fires early re-arms itself and sends nothing;
  the ledger     the hop sits upstream of the idempotency ledger and does not
                 weaken it: two identical tasks still send once;
  the commit     a dated loop a month out commits, with a link, whatever the
                 queue does.

The first half is pure. The second half drives core/chaser.py and
core/registrar.py, which reach the cloud SDK at import, so it skips on a laptop
with none and runs in the image, exactly as tests/test_chaser.py does.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import tasks

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
DAY = 24 * 3600


class TheClamp(unittest.TestCase):
    def test_the_ceiling_is_inside_the_720_hours_cloud_tasks_allows(self) -> None:
        self.assertLess(tasks.MAX_DELAY_SECONDS, 720 * 3600)
        self.assertEqual(tasks.MAX_DELAY_SECONDS, 28 * DAY)

    def test_a_delay_inside_the_ceiling_is_left_alone(self) -> None:
        delay, hops = tasks.hop(27 * DAY)
        self.assertEqual(delay, 27 * DAY)
        self.assertFalse(hops)

    def test_a_delay_past_the_ceiling_becomes_a_hop(self) -> None:
        delay, hops = tasks.hop(60 * DAY)
        self.assertEqual(delay, tasks.MAX_DELAY_SECONDS)
        self.assertTrue(hops)

    def test_a_negative_delay_is_now_and_not_a_time_machine(self) -> None:
        self.assertEqual(tasks.hop(-5), (0.0, False))

    def test_a_hop_remembers_the_moment_it_is_really_for(self) -> None:
        body = tasks.body_for({"kind": "nudge"}, 60 * DAY, now=NOW)
        self.assertEqual(tasks.due_at(body), NOW + timedelta(days=60))
        self.assertEqual(body["kind"], "nudge")

    def test_a_task_that_can_wait_for_itself_carries_no_flag(self) -> None:
        body = tasks.body_for({"kind": "nudge"}, 3 * DAY, now=NOW)
        self.assertNotIn(tasks.NOT_BEFORE, body)
        self.assertIsNone(tasks.due_at(body))

    def test_the_last_hop_drops_the_flag_so_it_actually_fires(self) -> None:
        """A hop whose remainder fits is the real thing, not another hop."""
        first = tasks.body_for({"kind": "nudge"}, 60 * DAY, now=NOW)
        second = tasks.body_for(first, 32 * DAY, now=NOW + timedelta(days=28))
        self.assertEqual(tasks.due_at(second), NOW + timedelta(days=60))
        last = tasks.body_for(second, 4 * DAY, now=NOW + timedelta(days=56))
        self.assertNotIn(tasks.NOT_BEFORE, last)

    def test_the_caller_is_payload_is_not_mutated(self) -> None:
        payload = {"kind": "nudge"}
        tasks.body_for(payload, 60 * DAY, now=NOW)
        self.assertEqual(payload, {"kind": "nudge"})

    def test_an_unreadable_moment_reads_as_due_now(self) -> None:
        """Fail closed: an unparseable flag means today's behaviour, not a stall."""
        for value in ("", "not a date", None, 17):
            with self.subTest(value=value):
                self.assertIsNone(tasks.due_at({tasks.NOT_BEFORE: value}))
        self.assertIsNone(tasks.due_at({}))

    def test_a_moment_written_without_a_zone_is_read_as_utc(self) -> None:
        when = tasks.due_at({tasks.NOT_BEFORE: "2026-09-30T10:00:00"})
        self.assertEqual(when, datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc))


# The rest imports core.chaser and core.registrar, which reach the cloud SDK.
try:
    from core import chaser, coordinator, events as events_module, lang, links
    from core import registrar, settings, store as store_module, telegram
    from core.models import Doctor, LinkToken, Loop, Patient, PendingConfirm
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ALoopSixtyDaysOutIsQueuedInHops(unittest.IsolatedAsyncioTestCase):
    """The whole ladder of a far-away loop, through the real enqueue path."""

    def setUp(self) -> None:
        outer = self
        self.created: list = []

        def create_task(path, payload, delay):
            # Cloud Tasks itself, in one line: this is the refusal that made
            # Confirm return 500.
            if delay > 720 * 3600:
                raise ValueError(
                    "400 The Task.scheduleTime is too far in the future. "
                    "Schedule time must be no more than 720h in the future."
                )
            outer.created.append({"path": path, "payload": payload, "delay": delay})
            return f"task/{len(outer.created)}"

        async def current():
            return "run1", 86400

        self.patches = [
            patch.object(tasks, "_create_task", create_task),
            patch.object(tasks, "configured", lambda: True),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(settings, "current", current),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def loop(self, days: int) -> Loop:
        return Loop(id="l", patient_id="p", doctor_id="d", type="VISIT",
                    title="Follow-up visit", due_at=NOW + timedelta(days=days),
                    created_at=NOW, updated_at=NOW)

    async def test_a_sixty_day_loop_schedules_hops_and_not_a_refusal(self) -> None:
        # `not_before` is stamped from the queue's own clock, which is the wall
        # clock and not the demo clock, so the ladder is checked as offsets.
        before = datetime.now(timezone.utc)
        queued = await chaser.schedule_loop(self.loop(60))
        self.assertEqual(len(queued), 3)
        for row in self.created:
            with self.subTest(delay=row["delay"]):
                self.assertEqual(row["delay"], tasks.MAX_DELAY_SECONDS)
                self.assertIn(tasks.NOT_BEFORE, row["payload"])
        offsets = [
            (tasks.due_at(row["payload"]) - before).total_seconds() / DAY
            for row in self.created
        ]
        for got, wanted in zip(offsets, (58, 60, 63)):
            with self.subTest(wanted=wanted):
                self.assertAlmostEqual(got, wanted, places=2)

    async def test_a_month_out_is_the_demos_own_sentence_and_it_queues(self) -> None:
        """"Come back in a month": due 30, third rung 33, both past the ceiling."""
        await chaser.schedule_loop(self.loop(30))
        self.assertEqual(len(self.created), 3)
        self.assertEqual([row["delay"] for row in self.created],
                         [28 * DAY, tasks.MAX_DELAY_SECONDS,
                          tasks.MAX_DELAY_SECONDS])

    async def test_a_near_loop_is_untouched_and_carries_no_flag(self) -> None:
        await chaser.schedule_loop(self.loop(14))
        self.assertEqual([row["delay"] for row in self.created],
                         [12 * DAY, 14 * DAY, 17 * DAY])
        for row in self.created:
            with self.subTest(delay=row["delay"]):
                self.assertNotIn(tasks.NOT_BEFORE, row["payload"])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheHopSendsNothing(unittest.IsolatedAsyncioTestCase):
    """A task that fires before its moment re-arms, and says so."""

    def setUp(self) -> None:
        outer = self
        self.sent: list = []
        self.written: list = []
        self.queued: list = []
        self.claimed: set = set()
        self.receipts: dict = {}
        self.contacts: list = []
        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="VISIT",
                         title="Follow-up visit", state="waiting_patient",
                         due_at=NOW + timedelta(days=60),
                         created_at=NOW, updated_at=NOW)

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))

        async def enqueue(path, payload, delay):
            outer.queued.append((path, dict(payload), delay))
            return f"task/{len(outer.queued)}"

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append((kind, text, kw.get("meta", {})))
            return None

        async def claim_send(send):
            if send.id in outer.claimed:
                return store_module.ALREADY_SENT
            outer.claimed.add(send.id)
            return store_module.CLAIMED

        async def mark_send(send_id, state, error=""):
            outer.receipts[send_id] = (state, error)

        async def note_contact(patient_id, doctor_id, day_index, kind,
                               loop_id=""):
            outer.contacts.append((patient_id, day_index, kind))
            return len(outer.contacts)

        async def contacted_on(patient_id, day_index):
            return any(row[0] == patient_id and row[1] == day_index
                       for row in outer.contacts)

        async def add_contact(loop_id, day_index):
            outer.loop.contacts = int(outer.loop.contacts or 0) + 1
            if day_index not in (outer.loop.contact_days or []):
                outer.loop.contact_days = [*(outer.loop.contact_days or []),
                                           day_index]

        async def add_reluctance(loop_id):
            outer.loop.reluctance = int(outer.loop.reluctance or 0) + 1
            return outer.loop.reluctance

        async def refund_contact(loop_id):
            outer.loop.contacts = max(0, int(outer.loop.contacts or 0) - 1)
            return outer.loop.contacts

        async def refund_day(patient_id, day_index, loop_id=""):
            for row in list(outer.contacts):
                if row[0] == patient_id and row[1] == day_index:
                    outer.contacts.remove(row)
                    break
            return 0

        async def reserve_contact(patient_id, doctor_id, day_index, loop_id,
                                  kind, *, max_contacts=None,
                                  allow_same_day=False):
            """The S12 reservation: both guards read and both budgets spent."""
            if not allow_same_day and await contacted_on(patient_id, day_index):
                return {"ok": False, "why": store_module.NO_DAY_LEFT}
            contacts = int(outer.loop.contacts or 0)
            if max_contacts is not None and contacts >= max_contacts:
                return {"ok": False, "why": store_module.NO_CONTACTS_LEFT}
            count = await note_contact(patient_id, doctor_id, day_index, kind,
                                       loop_id=loop_id)
            await add_contact(loop_id, day_index)
            return {"ok": True, "count": count, "contacts": contacts + 1}

        async def claim_delivery(loop_id, schedule_version, generation, at):
            if int(outer.loop.schedule_version or 0) != int(schedule_version):
                return None
            if int(outer.loop.generation or 0) != int(generation):
                return None
            outer.loop.attempts = int(outer.loop.attempts or 0) + 1
            outer.loop.state = "waiting_patient"
            outer.loop.last_attempt_at = at
            return outer.loop.attempts

        async def contact_days_for_patient(patient_id):
            return tuple(row[1] for row in outer.contacts
                         if row[0] == patient_id)

        async def bump_generation(loop_id):
            outer.loop.generation = int(outer.loop.generation or 0) + 1
            outer.loop.attempts = 0
            return outer.loop.generation

        async def current():
            return "run1", 86400

        async def get_loop(loop_id):
            return outer.loop if loop_id == outer.loop.id else None

        async def get_patient(patient_id):
            return outer.patient

        async def doctor_by_id(doctor_id):
            return outer.doctor

        async def sends_for_patient(patient_id):
            return []

        async def update_loop(loop_id, **fields):
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def for_patient(*a, **kw):
            return "ar"

        async def on_wake(*a, **kw):
            return None  # the ladder runs, which is the S3 behaviour

        self.patches = [
            patch.object(chaser, "fanout", lambda: Fanout()),
            patch.object(tasks, "enqueue", enqueue),
            patch.object(events_module, "append_event", append_event),
            patch.object(settings, "current", current),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "get_loop", get_loop),
            patch.object(store_module, "get_patient", get_patient),
            patch.object(store_module, "doctor_by_id", doctor_by_id),
            patch.object(store_module, "sends_for_patient", sends_for_patient),
            patch.object(store_module, "update_loop", update_loop),
            patch.object(store_module, "claim_send", claim_send),
            patch.object(store_module, "mark_send", mark_send),
            patch.object(store_module, "note_contact", note_contact),
            patch.object(store_module, "contacted_on", contacted_on),
            patch.object(store_module, "add_contact", add_contact),
            patch.object(store_module, "refund_contact", refund_contact),
            patch.object(store_module, "refund_day", refund_day),
            patch.object(store_module, "reserve_contact", reserve_contact),
            patch.object(store_module, "claim_delivery", claim_delivery),
            patch.object(store_module, "add_reluctance", add_reluctance),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
            patch.object(store_module, "bump_generation", bump_generation),
            patch.object(lang, "for_patient", for_patient),
            patch.object(coordinator, "on_wake", on_wake),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def payload(self, **extra) -> dict:
        return {"kind": "nudge", "run_id": "run1", "loop_id": "l", "attempt": 1,
                **extra}

    async def test_a_hop_re_arms_for_what_is_left_and_sends_nothing(self) -> None:
        due = NOW + timedelta(days=32)
        result = await chaser.fire(
            self.payload(**{tasks.NOT_BEFORE: due.isoformat()})
        )
        self.assertEqual(result["sent"], False)
        self.assertEqual(result["reason"], "re-armed")
        self.assertEqual(len(self.queued), 1)
        self.assertAlmostEqual(self.queued[0][2], 32 * DAY, places=3)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.claimed, set())

    async def test_the_event_says_the_date_it_was_re_armed_for(self) -> None:
        due = NOW + timedelta(days=32)
        await chaser.fire(self.payload(**{tasks.NOT_BEFORE: due.isoformat()}))
        texts = [text for _, text, _ in self.written]
        self.assertIn(f"re-armed for {due:%Y-%m-%d}", texts)
        meta = [m for _, t, m in self.written if t.startswith("re-armed")][0]
        self.assertEqual(meta["not_before"], due.isoformat())
        self.assertIn("720 hours", meta["audit"]["line"])

    async def test_the_hop_that_arrives_on_time_sends_the_nudge(self) -> None:
        """The last hop is not a hop: its moment has come, so it works."""
        result = await chaser.fire(
            self.payload(**{tasks.NOT_BEFORE: (NOW - timedelta(days=1)).isoformat()})
        )
        self.assertTrue(result["sent"])
        self.assertTrue(any(ref.startswith("patient:") for ref, _, _ in self.sent))

    async def test_a_task_with_no_flag_behaves_exactly_as_it_always_did(self) -> None:
        result = await chaser.fire(self.payload())
        self.assertTrue(result["sent"])

    async def test_the_replay_ledger_still_refuses_a_duplicate(self) -> None:
        """The hop sits upstream of the ledger and must not weaken it.

        Forced, for the reason the live replay was forced: an unforced second
        task is stopped a rule earlier, by "one message per patient per day",
        and never reaches the ledger at all.
        """
        first = await chaser.fire(self.payload(force=True))
        second = await chaser.fire(self.payload(force=True))
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], "already sent")

    async def test_a_replayed_hop_re_arms_twice_but_still_sends_nothing(self) -> None:
        """Two copies of one hop cost two tasks and zero messages.

        Worth saying out loud: a re-arm is not a contact, so it is not claimed
        in the ledger, and a Cloud Tasks retry of a hop therefore re-arms again.
        That is a task on the queue, not a message to the patient, and the
        ledger still stands in front of everything that is a message.
        """
        payload = self.payload(
            **{tasks.NOT_BEFORE: (NOW + timedelta(days=32)).isoformat()}
        )
        await chaser.fire(payload)
        await chaser.fire(payload)
        self.assertEqual(len(self.queued), 2)
        self.assertEqual(self.sent, [])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ConfirmMintsTheLinkWhateverTheQueueDoes(unittest.IsolatedAsyncioTestCase):
    """The record outlives the queue: the defect that killed beat 1."""

    def setUp(self) -> None:
        outer = self
        self.sent: list = []
        self.loops: list = []
        self.tokens: list = []
        self.refuse_everything = False
        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             created_at=NOW)
        self.proposed = {
            "patient": {"name": "Ahmed Ali", "phone": "0100 000 0011", "age": 58,
                        "sex": "male", "diagnosis": "heart failure"},
            "baseline": [], "targets": [],
            "plan_text": "Come back in a month.",
            "loops": [{"type": "VISIT", "title": "Follow-up visit",
                       "due_in_days": 30}],
        }

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))

        def create_task(path, payload, delay):
            if outer.refuse_everything:
                raise ValueError("400 The queue refused this task.")
            if delay > 720 * 3600:
                raise ValueError(
                    "400 The Task.scheduleTime is too far in the future."
                )
            return "task/1"

        async def get_confirm(confirm_id):
            return PendingConfirm(id=confirm_id, doctor_id="d",
                                  proposed=outer.proposed,
                                  expires_at=NOW + timedelta(hours=6))

        async def claim_confirm(confirm_id):
            return True   # nothing else is racing this proposal here

        async def release_confirm(confirm_id):
            return None

        async def create_patient(patient):
            outer.patient = patient
            return patient

        async def create_loop(loop):
            outer.loops.append(loop)
            return loop

        async def list_loops(patient_id):
            return list(outer.loops)

        async def save_link_token(token):
            outer.tokens.append(token)
            return token

        async def deep_link(token_id):
            return f"https://t.me/SanadHealthBot?start={token_id}"

        async def nothing(*a, **kw):
            return None

        async def current():
            return "run1", 86400

        counter = {"n": 0}

        def new_id():
            counter["n"] += 1
            return f"id{counter['n']}"

        self.patches = [
            patch.object(registrar, "fanout", lambda: Fanout()),
            patch.object(tasks, "_create_task", create_task),
            patch.object(tasks, "configured", lambda: True),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "new_id", new_id),
            patch.object(store_module, "get_confirm", get_confirm),
            patch.object(store_module, "claim_confirm", claim_confirm),
            patch.object(store_module, "release_confirm", release_confirm),
            patch.object(store_module, "create_patient", create_patient),
            patch.object(store_module, "create_loop", create_loop),
            patch.object(store_module, "list_loops", list_loops),
            patch.object(store_module, "save_link_token", save_link_token),
            patch.object(store_module, "delete_confirm", nothing),
            patch.object(store_module, "add_event", nothing),
            patch.object(events_module, "append_event", nothing),
            patch.object(settings, "current", current),
            patch.object(telegram, "deep_link", deep_link),
            patch.object(telegram, "enabled", lambda: False),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def cards(self) -> list:
        return [card for _, _, card in self.sent if card]

    async def test_come_back_in_a_month_commits_with_a_link(self) -> None:
        await registrar.commit(self.doctor, "c1", "https://sanad.example")
        committed = [c for c in self.cards() if c["title"] == "Committed."]
        self.assertEqual(len(committed), 1)
        self.assertTrue(any("Patient link:" in line
                            for line in committed[0]["lines"]))
        self.assertTrue(any("3 reminders scheduled." in line
                            for line in committed[0]["lines"]))
        self.assertEqual(len(self.tokens), 1)

    async def test_a_queue_that_refuses_is_a_card_and_not_a_lost_patient(self) -> None:
        self.refuse_everything = True
        await registrar.commit(self.doctor, "c1", "https://sanad.example")
        committed = [c for c in self.cards() if c["title"] == "Committed."]
        self.assertEqual(len(committed), 1)
        self.assertTrue(any("Patient link:" in line
                            for line in committed[0]["lines"]))
        warned = [c for c in self.cards()
                  if c["title"].startswith("Reminders not scheduled")]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0]["severity"], "yellow")
        self.assertTrue(any("force_due" in line for line in warned[0]["lines"]))

    async def test_the_link_is_minted_before_the_queue_is_touched(self) -> None:
        """Order, in the source: nothing after the mint may take it away."""
        from pathlib import Path

        source = (Path(registrar.__file__).read_text(encoding="utf-8")
                  .split("async def commit(", 1)[1])
        self.assertLess(source.index("links.mint("),
                        source.index("chaser.schedule_patient("))


if __name__ == "__main__":
    unittest.main()
