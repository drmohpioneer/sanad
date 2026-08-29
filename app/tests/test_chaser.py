"""/force_due parses its own argument, so the demo never forces the wrong loop.

S3 review, carry-over 2: a patient with two open loops needs the doctor to be
able to say which one. The split is decided against the real names on the board,
which is why it is a function of both, and why it is tested here.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from core.models import Loop

try:  # core.chaser reaches Firestore at import; the image has it, a laptop may not
    from core import chaser
except ImportError as exc:  # pragma: no cover - the image build always has it
    raise unittest.SkipTest(f"cloud SDK not installed: {exc}") from exc

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
NAMES = ["Ahmed Ali", "Ismail Roshdy", "Hend Ismail"]


def loop(title: str, kind: str = "TEST", **details: str) -> Loop:
    return Loop(id=title, patient_id="p", doctor_id="d", type=kind, title=title,
                details=details, created_at=NOW, updated_at=NOW)


class SplittingTheArgument(unittest.TestCase):
    def test_a_bare_name_stays_a_name(self) -> None:
        self.assertEqual(chaser.split_argument("Ahmed", NAMES), ("ahmed", ""))
        self.assertEqual(chaser.split_argument("Ahmed Ali", NAMES), ("ahmed ali", ""))

    def test_a_loop_word_is_separated_from_the_name(self) -> None:
        self.assertEqual(chaser.split_argument("Ahmed lipid", NAMES), ("ahmed", "lipid"))
        self.assertEqual(
            chaser.split_argument("Ismail Roshdy kidney", NAMES),
            ("ismail roshdy", "kidney"),
        )

    def test_the_longest_matching_name_wins(self) -> None:
        """"Ahmed Ali" is a patient, so it is not read as name + loop word."""
        head, word = chaser.split_argument("Ahmed Ali lipid", NAMES)
        self.assertEqual((head, word), ("ahmed ali", "lipid"))

    def test_an_unknown_name_is_left_whole_for_the_error_message(self) -> None:
        self.assertEqual(chaser.split_argument("Someone Else", NAMES),
                         ("someone else", ""))


class MatchingTheLoop(unittest.TestCase):
    def test_the_title_matches(self) -> None:
        self.assertTrue(chaser.matches_loop(loop("Lipid panel"), "lipid"))
        self.assertFalse(chaser.matches_loop(loop("Lipid panel"), "kidney"))

    def test_the_details_match_too(self) -> None:
        """A doctor says "potassium" and the loop is titled "Electrolytes"."""
        electrolytes = loop("Electrolytes", test_name="potassium and sodium")
        self.assertTrue(chaser.matches_loop(electrolytes, "potassium"))

    def test_the_type_matches(self) -> None:
        self.assertTrue(chaser.matches_loop(loop("Blood pressure", "MONITOR"),
                                            "monitor"))


# --------------------------------------------------------------------------- #
# rev 17, item 1: a replayed Cloud Task must cost nothing
# --------------------------------------------------------------------------- #
try:
    from core import coordinator, events as events_module, lang, policy, settings
    from core import store as store_module, tasks
    from core.models import Doctor, Patient
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AReplayedWakeUpIsRefusedBeforeTheModel(unittest.IsolatedAsyncioTestCase):
    """The same task twice: one model call, one message, one receipt.

    Until rev 17 the idempotency claim happened after the Coordinator turn, so
    a Cloud Tasks retry woke the agent a second time, paid for a second model
    call, and could send a second patient template that the ledger never saw.
    The claim now sits in front of the turn, so the second copy is refused with
    the receipt id and never reaches the model at all.
    """

    def setUp(self) -> None:
        outer = self
        self.sent: list = []
        self.written: list = []
        self.claimed: set = set()
        self.receipts: dict = {}
        self.contacts: list = []
        self.model_calls = 0
        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                         title="Lipid panel", state="waiting_patient",
                         due_at=NOW + timedelta(days=3),
                         created_at=NOW, updated_at=NOW)

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.meta or {}))

        async def enqueue(path, payload, delay):
            return "task/1"

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

        async def refund_day(patient_id, day_index, loop_id=""):
            for row in list(outer.contacts):
                if row[0] == patient_id and row[1] == day_index:
                    outer.contacts.remove(row)
                    break
            return 0

        async def add_contact_kind(patient_id, day_index, kind):
            outer.contacts.append((patient_id, day_index, kind))

        async def refund_contact(loop_id):
            outer.loop.contacts = max(0, int(outer.loop.contacts or 0) - 1)
            return outer.loop.contacts

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

        async def stub_model(turn):
            """The model, stubbed: it always asks to record the same barrier."""
            outer.model_calls += 1
            turn.propose("classify_barrier", {"barrier": "availability"},
                         "the lab is closed until Sunday")
            return turn.decision

        self.patches = [
            patch.object(chaser, "fanout", lambda: Fanout()),
            patch.object(coordinator, "fanout", lambda: Fanout()),
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
            patch.object(store_module, "add_contact_kind", add_contact_kind),
            patch.object(store_module, "reserve_contact", reserve_contact),
            patch.object(store_module, "claim_delivery", claim_delivery),
            patch.object(store_module, "add_reluctance", add_reluctance),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
            patch.object(store_module, "bump_generation", bump_generation),
            patch.object(lang, "for_patient", for_patient),
            patch.object(coordinator, "_choose", stub_model),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def payload(self, **extra) -> dict:
        return {"kind": "nudge", "run_id": "run1", "loop_id": "l", "attempt": 1,
                "force": True, **extra}

    def to_patient(self) -> list:
        return [row for row in self.sent if row[0].startswith("patient:")]

    async def test_the_same_task_twice_costs_one_model_call_and_one_message(self) -> None:
        first = await chaser.fire(self.payload())
        second = await chaser.fire(self.payload())

        self.assertEqual(self.model_calls, 1)
        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(first["reason"], "coordinator: classify_barrier")
        self.assertEqual(second["sent"], False)
        self.assertEqual(second["reason"], "already sent")
        self.assertEqual(second["key"], "l:0:nudge:1")

    async def test_both_answers_name_the_same_receipt(self) -> None:
        first = await chaser.fire(self.payload())
        second = await chaser.fire(self.payload())
        self.assertEqual(first["key"], second["key"])

    async def test_what_the_agent_said_carries_that_receipt(self) -> None:
        """The receipt is on the message, not only on the ladder nudge."""
        await chaser.fire(self.payload())
        ref, text, meta = self.to_patient()[0]
        self.assertEqual(meta["audit"]["receipt"], "l:0:nudge:1")
        self.assertEqual(meta["audit"]["tier"], "coordinator")
        self.assertTrue(text)

    async def test_the_coordinator_event_carries_it_too(self) -> None:
        await chaser.fire(self.payload())
        metas = [m for _, t, m in self.written if t.startswith("coordinator:")]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["receipt"], "l:0:nudge:1")

    async def test_a_stand_down_gives_the_ladder_the_same_claim(self) -> None:
        """The agent choosing nothing must still leave exactly one nudge."""
        async def chooses_nothing(turn):
            outer_calls.append(1)
            return None

        outer_calls: list = []
        with patch.object(coordinator, "_choose", chooses_nothing):
            first = await chaser.fire(self.payload())
            second = await chaser.fire(self.payload())
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(second["reason"], "already sent")
        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(len(outer_calls), 1)


if __name__ == "__main__":
    unittest.main()
