"""Wave C: what a photo carries, what an escalation persists, what a page does.

Three groups, and they are separate because they need different things.

The first drives `core/extractor.handle_photo` against an in-memory store with
the model call stubbed, so the two facts the pregnancy rule needs, the ladder
that has to stand down when evidence lands, and the appends that must not lose
a concurrent write are all proved by running the code and not by reading it.

The second is the order of operations on an escalation: the record and the
doctor's card exist before the patient is told his doctor knows. Reading the
source is the honest test for two of those paths, because the sentence that has
to come second is inside a branch a stub cannot get to without a model.

The third reads `web/patient.html` and `web/dashboard.html` as text, the way
tests/test_dashboard_routes.py reads main.py: a page has no test runner here and
the guarantee is the shape of the handler.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from core.models import Doctor, Event, Loop, Patient

APP_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# The store double
# --------------------------------------------------------------------------- #
class FakeStore:
    """Enough Firestore for the photo path, in memory.

    `append_reading` and `append_result` are real unions here, not appends: the
    point of the store functions they stand in for is that a row added twice is
    one row, and a test that let them behave like a list append would not notice
    a caller that lost that.
    """

    def __init__(self) -> None:
        self.doctors: dict[str, Doctor] = {}
        self.patients: dict[str, Patient] = {}
        self.loops: dict[str, Loop] = {}
        self.events: list[Event] = []
        self.photo_receipts: dict[tuple[str, int, str], dict] = {}
        self.clock = NOW
        self.counter = 0

    def now(self) -> datetime:
        return self.clock

    def new_id(self) -> str:
        self.counter += 1
        return f"id{self.counter}"

    async def add_event(self, event: Event) -> Event:
        self.events.append(event)
        return event

    async def list_events(self, doctor_id: str) -> list[Event]:
        return [Event(**e.model_dump()) for e in self.events
                if e.doctor_id == doctor_id]

    async def get_patient(self, patient_id: str):
        row = self.patients.get(patient_id)
        return Patient(**row.model_dump()) if row else None

    async def doctor_by_id(self, doctor_id: str):
        row = self.doctors.get(doctor_id)
        return Doctor(**row.model_dump()) if row else None

    async def list_loops(self, patient_id: str) -> list[Loop]:
        return [Loop(**l.model_dump()) for l in self.loops.values()
                if l.patient_id == patient_id]

    async def get_loop(self, loop_id: str):
        row = self.loops.get(loop_id)
        return Loop(**row.model_dump()) if row else None

    async def update_loop(self, loop_id: str, **fields) -> None:
        row = self.loops[loop_id]
        self.loops[loop_id] = Loop(**{**row.model_dump(), **fields})

    async def append_reading(self, loop_id: str, row: dict) -> None:
        loop = self.loops[loop_id]
        rows = list(loop.readings or [])
        if row not in rows:
            rows.append(row)
        await self.update_loop(loop_id, readings=rows)

    async def append_result(self, loop_id: str, rows) -> None:
        loop = self.loops[loop_id]
        kept = list(loop.results or [])
        for row in ([rows] if isinstance(rows, dict) else list(rows)):
            if row not in kept:
                kept.append(row)
        await self.update_loop(loop_id, results=kept)

    async def bump_schedule_version(self, loop_id: str) -> int:
        loop = self.loops[loop_id]
        version = int(loop.schedule_version or 0) + 1
        await self.update_loop(loop_id, schedule_version=version)
        return version

    async def claim_photo(self, patient_id, day_index, digest, owner) -> bool:
        key = (patient_id, day_index, digest)
        if key in self.photo_receipts:
            return False
        self.photo_receipts[key] = {"owner": owner, "state": "claimed"}
        return True

    async def complete_photo(self, patient_id, day_index, digest, owner) -> None:
        row = self.photo_receipts.get((patient_id, day_index, digest))
        if row and row["owner"] == owner:
            row["state"] = "complete"

    async def release_photo(self, patient_id, day_index, digest, owner) -> None:
        key = (patient_id, day_index, digest)
        row = self.photo_receipts.get(key)
        if row and row["owner"] == owner:
            del self.photo_receipts[key]


STORE_NAMES = ("now", "new_id", "add_event", "list_events", "get_patient",
               "doctor_by_id", "list_loops", "get_loop", "update_loop",
               "append_reading", "append_result", "bump_schedule_version",
               "claim_photo", "complete_photo", "release_photo")


class Recorder:
    """A fanout that keeps what was sent instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def send(self, target_ref, msg):
        self.sent.append((target_ref, msg))
        return f"receipt{len(self.sent)}"

    def to(self, prefix: str) -> list[object]:
        return [m for ref, m in self.sent if ref.startswith(prefix)]


def slip(**kwargs):
    from core.models import PhotoReading, SlipAnalyte

    rows = kwargs.pop("analytes", [])
    return PhotoReading(
        kind=kwargs.pop("kind", "lab_slip"),
        text_orientation="upright",
        analytes=[SlipAnalyte(**r) for r in rows],
        **kwargs,
    )


try:  # pragma: no cover - the image build always has the SDK
    from core import escalate, extractor, registrar
    EXTRACTOR_MISSING = ""
except Exception as exc:  # pragma: no cover
    escalate = extractor = registrar = None
    EXTRACTOR_MISSING = f"the cloud SDK is not installed here: {exc}"


@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class ThePhotoPath(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from core import chaser, coordinator, labs, storage, store

        self.fake = FakeStore()
        self.doctor = Doctor(id="d", name="Dr Mohamed", specialty="obstetrics",
                             lang="en", web_token="tok", created_at=NOW)
        self.patient = Patient(id="p1", doctor_id="d", name="Mona Said",
                               sex="f", diagnosis="pregnancy",
                               channels={"web": True}, created_at=NOW)
        self.fake.doctors["d"] = self.doctor
        self.fake.patients["p1"] = self.patient
        self.fake.loops["l1"] = Loop(
            id="l1", patient_id="p1", doctor_id="d", type="TEST",
            title="Beta hCG", state="open", details={"test_name": "Beta hCG"},
            created_at=NOW - timedelta(days=2), updated_at=NOW)

        self.out = Recorder()
        self.superseded: list[tuple[str, str]] = []
        self.evidence: list[str] = []
        self.assessed: list[dict] = []

        for name in STORE_NAMES:
            self.enterContext(patch.object(store, name, getattr(self.fake, name)))
        self.enterContext(patch.object(extractor, "fanout", lambda: self.out))
        self.enterContext(patch.object(
            storage, "put_image",
            self._async(lambda *a, **k: "gs://labs/run/p1/x.jpg")))
        self.enterContext(patch.object(
            extractor.settings, "current", self._async(lambda: ("run1", 86400))))

        async def supersede(loop_id, reason):
            self.superseded.append((loop_id, reason))
            return await self.fake.bump_schedule_version(loop_id)

        self.enterContext(patch.object(chaser, "supersede_ladder", supersede))

        async def on_evidence(loop, patient, doctor, note=""):
            self.evidence.append(loop.id)

        self.enterContext(patch.object(coordinator, "on_evidence", on_evidence))

        real_assess = labs.assess

        def spy(analytes, targets=None, baseline=None, *, context=None):
            self.assessed.append({"context": context})
            return real_assess(analytes, targets, baseline, context=context)

        self.enterContext(patch.object(labs, "assess", spy))

    @staticmethod
    def _async(fn):
        async def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper

    def _read(self, reading):
        async def read_photo(image, mime="image/jpeg"):
            return reading, {}
        return patch.object(extractor, "read_photo", read_photo)

    def _said(self, text: str, hours_ago: float) -> None:
        self.fake.events.append(Event(
            id=self.fake.new_id(), doctor_id="d", patient_id="p1",
            kind="patient_in", text=text,
            ts=NOW - timedelta(hours=hours_ago)))

    # --- kernel review F1 -------------------------------------------------
    async def test_the_caption_and_the_last_48_hours_reach_the_table(self) -> None:
        """The pregnancy rule needs two facts and only one is on the slip."""
        self._said("عندي وجع في بطني من امبارح", hours_ago=6)
        self._said("this is from a week ago", hours_ago=24 * 7)
        reading = slip(analytes=[{"analyte": "Beta hCG", "value": "1200",
                                 "unit": "mIU/mL", "flag": "POSITIVE"}])
        with self._read(reading):
            await extractor.handle_photo(
                self.patient, self.doctor, b"x", caption="ده تحليل الحمل")

        self.assertEqual(len(self.assessed), 1)
        context = self.assessed[0]["context"]
        self.assertIsNotNone(context, "a context of None means nobody looked")
        self.assertIn("ده تحليل الحمل", context)
        self.assertIn("عندي وجع في بطني من امبارح", context)
        self.assertNotIn("this is from a week ago", context)

    async def test_a_silent_patient_is_looked_for_and_not_skipped(self) -> None:
        """None and [] are different facts and the doctor's card says which."""
        from core import labs

        reading = slip(analytes=[{"analyte": "LDL", "value": "150",
                                 "unit": "mg/dL"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        context = self.assessed[0]["context"]
        self.assertEqual(context, [])
        self.assertTrue(labs.context_searched(context))

    async def test_only_the_patients_own_words_are_read(self) -> None:
        """An outbound template is Sanad's sentence, never a reported symptom."""
        self.fake.events.append(Event(
            id="e9", doctor_id="d", patient_id="p1", kind="agent_out",
            text="do you have abdominal pain", ts=NOW - timedelta(hours=1)))
        reading = slip(analytes=[{"analyte": "LDL", "value": "150"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual(self.assessed[0]["context"], [])

    async def test_a_history_read_that_throws_keeps_the_caption(self) -> None:
        """codex item 11: no 500 on the photo path, and no lost caption."""
        from core import events as events_module

        async def boom(doctor_id, since_ms=0, limit=200):
            raise RuntimeError("firestore is down")

        with patch.object(events_module, "last_events", boom):
            words = await extractor.recent_words(
                self.patient, self.doctor, "ده تحليل الحمل")
        self.assertEqual(words, ["ده تحليل الحمل"])

    # --- kernel review F8b -------------------------------------------------
    async def test_evidence_supersedes_the_rungs_still_on_the_queue(self) -> None:
        reading = slip(analytes=[{"analyte": "Beta hCG", "value": "1200"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual([loop for loop, _ in self.superseded], ["l1"])

    async def test_a_slip_that_did_not_satisfy_stands_the_ladder_down_too(self) -> None:
        """wave A handoff 4: the sentence is wrong either way.

        The ladder rung says "please do the test". A patient who sent an unnamed
        slip has done the test; what is unresolved is whether it counts, and
        that is the doctor's question, not a reason to ask again.
        """
        self.fake.loops["l1"] = Loop(
            **{**self.fake.loops["l1"].model_dump(),
               "details": {"test_name": "Beta hCG", "analytes": ["Beta hCG",
                                                                 "Progesterone"]}})
        reading = slip(analytes=[{"analyte": "Beta hCG", "value": "1200"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual([loop for loop, _ in self.superseded], ["l1"])

    # --- codex item 13, the appends ---------------------------------------
    async def test_the_same_slip_twice_is_one_set_of_rows(self) -> None:
        reading = slip(analytes=[{"analyte": "Beta hCG", "value": "1200",
                                 "unit": "mIU/mL"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual(len(self.fake.loops["l1"].results), 1)
        self.assertTrue(any(e.meta.get("duplicate_image") for e in self.fake.events))
        self.assertTrue(any("already received" in m.text for m in self.out.to("patient:")))

    async def test_a_second_slip_does_not_erase_the_first(self) -> None:
        """A partial slip followed by the missing half keeps both halves."""
        with self._read(slip(analytes=[{"analyte": "Beta hCG", "value": "1200"}])):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        with self._read(slip(analytes=[{"analyte": "Progesterone",
                                        "value": "18"}])):
            await extractor.handle_photo(self.patient, self.doctor, b"y")
        names = [r["analyte"] for r in self.fake.loops["l1"].results]
        self.assertIn("Beta hCG", names)
        self.assertIn("Progesterone", names)

    # --- codex item 10, driven rather than read -------------------------
    async def test_a_critical_value_tells_the_patient_after_the_doctor(self) -> None:
        """The escalation and the red card exist before the promise is made."""
        reading = slip(analytes=[{"analyte": "Potassium", "value": "6.4",
                                 "unit": "mmol/L", "flag": "H"}])
        with self._read(reading):
            await extractor.handle_photo(self.patient, self.doctor, b"x")

        to_patient = [m.text for ref, m in self.out.sent
                      if ref.startswith("patient:")]
        self.assertTrue(any("123" in t for t in to_patient), to_patient)
        kinds = [e.kind for e in self.fake.events]
        self.assertIn("escalation", kinds)
        # The doctor's card and the escalation are both in front of the line
        # the patient reads.
        order = [ref for ref, _ in self.out.sent]
        self.assertLess(order.index("doctor:tok"), order.index("patient:p1"))

    async def test_an_escalation_that_cannot_be_written_says_so(self) -> None:
        """codex item 10, the half that matters.

        A Firestore timeout between the reassurance and the record used to leave
        a patient told to stop waiting and a doctor who knew nothing. Now the
        write comes first, and when it throws the patient is told Sanad could
        not reach his doctor. The instruction to go to hospital is kept: that
        never depended on the doctor hearing anything.
        """
        from core import escalate, events as events_module

        real = events_module.append_event
        calls = {"n": 0}

        async def flaky(doctor_id, kind, text="", **kwargs):
            calls["n"] += 1
            if text.startswith("emergency: critical lab value"):
                raise RuntimeError("firestore is down")
            return await real(doctor_id, kind, text, **kwargs)

        reading = slip(analytes=[{"analyte": "Potassium", "value": "6.4",
                                 "unit": "mmol/L", "flag": "H"}])
        with self._read(reading), patch.object(events_module, "append_event",
                                               flaky):
            await extractor.handle_photo(self.patient, self.doctor, b"x")

        told = [m for ref, m in self.out.sent if ref.startswith("patient:")]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0].text,
                         escalate.fail_closed_text("en", "f", emergency=True))
        self.assertIn("123", told[0].text, "he is still sent to hospital")
        self.assertNotIn("دكتورك اتبلغ", told[0].text)
        self.assertEqual(told[0].meta["audit"]["error"], escalate.FAIL_CLOSED)
        # And the failure is on the board, not only in a log.
        self.assertTrue(any(e.kind == "escalation" and "FAILED to record" in e.text
                            for e in self.fake.events),
                        [e.text for e in self.fake.events])

    async def test_a_monitor_reading_is_appended_and_not_rewritten(self) -> None:
        self.fake.loops["m1"] = Loop(
            id="m1", patient_id="p1", doctor_id="d", type="MONITOR",
            title="Blood pressure", state="open",
            details={"metric": "BP", "schedule": "twice a day", "days": 7},
            readings=[{"at": "2026-08-28T09:00", "value": "120/80",
                       "number": 120.0}],
            created_at=NOW - timedelta(days=1), updated_at=NOW)
        with self._read(slip(kind="bp_monitor", systolic="118",
                             diastolic="76", pulse="70")):
            await extractor.handle_photo(self.patient, self.doctor, b"x")
        self.assertEqual(len(self.fake.loops["m1"].readings), 2)


# --------------------------------------------------------------------------- #
# Item 11: every patient-facing dependency is bounded and fails closed
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class NothingAPatientWaitsOnRunsForEver(unittest.IsolatedAsyncioTestCase):
    """A dependency that hangs and one that throws are the same defect.

    Each of the five is driven with a stub that raises and, where the deadline
    is the point, with one that sleeps past it. The assertion is always the
    same: the patient gets a sentence, the doctor gets a record, and nothing
    reaches the caller as an exception, because the caller is a FastAPI route
    and an exception there is a 500 the patient reads as "something broke".
    """

    async def test_the_helper_cancels_what_it_gave_up_on(self) -> None:
        import asyncio

        from core import bounded

        started = asyncio.Event()

        async def hangs():
            started.set()
            await asyncio.sleep(30)

        with self.assertRaises(bounded.TimedOut):
            await bounded.within(0.05, hangs(), what="a test")
        self.assertTrue(started.is_set())

    async def test_the_triage_vote_that_hangs_fails_closed_to_a_relay(self) -> None:
        import asyncio

        from core import sentinel

        async def hangs(text):
            await asyncio.sleep(30)
            return True

        with patch.object(sentinel, "model_net", hangs), \
                patch.object(sentinel.bounded, "TRIAGE", 0.05):
            verdict = await sentinel.check("I feel a bit tired today")
        self.assertTrue(verdict.fired)
        self.assertTrue(verdict.unavailable)
        self.assertEqual(verdict.net, sentinel.MODEL_ERROR_NET)

    async def test_both_output_votes_fail_closed_to_a_relay(self) -> None:
        from core import validator

        async def boom(*args, **kwargs):
            raise RuntimeError("vertex is down")

        with patch.object(validator, "_yes_no", boom):
            self.assertTrue(await validator.model_change_vote("can I take two?"))
            self.assertTrue(await validator.model_reassurance_vote("all normal"))

    async def test_a_photo_read_that_throws_is_a_card_and_not_a_crash(self) -> None:
        from core import extractor as ex

        async def boom(*args, **kwargs):
            raise RuntimeError("vertex is down")

        with patch.object(ex.media.client.aio.models, "generate_content", boom):
            reading, note = await ex.read_photo(_one_pixel_jpeg())
        self.assertIsNone(reading)
        self.assertIn("error", note)

    async def test_a_bucket_that_is_down_still_lets_the_values_through(self) -> None:
        from core import bounded

        async def boom(*args, **kwargs):
            raise RuntimeError("the bucket is down")

        path = await bounded.or_none(1.0, boom(), what="storing the photo")
        self.assertIsNone(path)

    async def test_the_card_says_when_the_picture_was_not_kept(self) -> None:
        from core import extractor as ex

        card = ex.unexpected_card(
            Patient(id="p1", doctor_id="d", name="Ahmed", created_at=NOW), "",
            "not readable")
        self.assertIn("image not stored", " ".join(card["lines"]))

    async def test_a_concierge_outage_relays_instead_of_answering(self) -> None:
        """The one model-written sentence: an outage takes the relay path."""
        from core import concierge

        turn = (APP_ROOT / "core" / "concierge.py").read_text("utf-8").split(
            "# Gate 2 - the Concierge", 1)[1].split("# Gate 3 -", 1)[0]
        self.assertIn("bounded.within(", turn)
        self.assertIn("bounded.TEXT", turn)
        self.assertIn('tier="relay"', turn)
        self.assertIn("MODEL_UNAVAILABLE_REASON", turn)
        self.assertTrue(concierge.MODEL_UNAVAILABLE_REASON.strip())

    async def test_a_transcription_that_fails_asks_for_the_message_again(self) -> None:
        from core import concierge

        for who in ("m", "f", "u"):
            self.assertTrue(
                concierge.voice_unreadable_text("ar", who).strip())
        self.assertIn("voice note", concierge.voice_unreadable_text("en", "u"))
        card = concierge.voice_unreadable_card(
            Patient(id="p1", doctor_id="d", name="Ahmed", created_at=NOW),
            "it timed out")
        self.assertEqual(card["severity"], "yellow")
        self.assertIn("it timed out", " ".join(card["lines"]))

    def test_the_deadline_table_covers_every_dependency(self) -> None:
        from core import bounded

        for name in ("TRIAGE", "VOTE", "TEXT", "TRANSCRIBE", "PHOTO", "STORAGE"):
            self.assertGreater(getattr(bounded, name), 0, name)

    def test_the_two_transcription_lanes_are_both_bounded(self) -> None:
        for where in ("dispatch.py", "registrar.py"):
            text = (APP_ROOT / "core" / where).read_text("utf-8")
            self.assertNotIn("await media.transcribe_async(", text, where)
            self.assertIn("bounded.TRANSCRIBE", text, where)


def _one_pixel_jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="JPEG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Item 10: the record exists before the patient is told
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class TheDoctorIsToldBeforeThePatientHearsIt(unittest.TestCase):
    """codex item 10, read as source: the order of two calls in one branch.

    "Your doctor has just been alerted" is a promise, and until this it was made
    before anything durable existed. Every one of these four branches now writes
    the escalation, opens the relay where there is one, and puts the card in
    front of the doctor first; only then does the patient hear it. A write that
    throws never reaches the promise at all: `_told_or_fail_closed` catches it,
    the patient gets the line that says Sanad could not reach the doctor, and an
    error event is written.
    """

    def setUp(self) -> None:
        self.concierge = (APP_ROOT / "core" / "concierge.py").read_text("utf-8")
        self.extractor = (APP_ROOT / "core" / "extractor.py").read_text("utf-8")
        self.coordinator = (APP_ROOT / "core" / "coordinator.py").read_text("utf-8")

    def _branch(self, text: str, start: str, end: str) -> str:
        return text.split(start, 1)[1].split(end, 1)[0]

    def _persists_before_it_promises(self, branch: str, where: str) -> None:
        """The one shape all four branches share.

        The persistence is a closure, the closure is handed to
        `escalate.told_or_fail_closed`, and the sentence to the patient is
        written after that call and reads its answer. So the order is the order
        of two indices, and the fallback cannot be forgotten: the send is a
        conditional expression on `landed`.
        """
        self.assertIn("escalate.told_or_fail_closed(", branch, where)
        self.assertLess(branch.index("escalate.told_or_fail_closed("),
                        branch.index("out.send(to_patient"), where)
        self.assertIn("escalate.fail_closed_text(", branch, where)
        self.assertIn("landed", branch, where)

    def test_the_emergency_branch_persists_first(self) -> None:
        branch = self._branch(self.concierge, "    if gate.fired:\n",
                              "# Gate 2b")
        self._persists_before_it_promises(branch, "concierge emergency")
        self.assertLess(branch.index("red_card("),
                        branch.index("escalate.told_or_fail_closed("))

    def test_the_triage_outage_relay_persists_first(self) -> None:
        branch = self._branch(self.concierge,
                              "if gate.fired and gate.unavailable:",
                              "    if gate.fired:\n")
        self._persists_before_it_promises(branch, "concierge triage outage")
        self.assertLess(branch.index("open_relay("),
                        branch.index("escalate.told_or_fail_closed("))

    def test_the_ordinary_relay_persists_first(self) -> None:
        branch = self._branch(self.concierge, "async def relay_to_doctor(",
                              "async def recent_history(")
        self._persists_before_it_promises(branch, "concierge relay_to_doctor")
        self.assertLess(branch.index("open_relay("),
                        branch.index("escalate.told_or_fail_closed("))

    def test_the_critical_lab_branch_persists_first(self) -> None:
        branch = self._branch(self.extractor, "async def persist_critical(",
                              "    # Not critical")
        self._persists_before_it_promises(branch, "extractor critical lab")
        self.assertLess(branch.index('"escalation"'),
                        branch.index("escalate.told_or_fail_closed("))

    def test_the_red_blood_pressure_branch_persists_first(self) -> None:
        branch = self._branch(self.extractor, "async def escalate_bp(",
                              "async def _relay_unread(")
        self._persists_before_it_promises(branch, "extractor escalate_bp")
        self.assertLess(branch.index("vitals.red_card("),
                        branch.index("escalate.told_or_fail_closed("))

    def test_the_cost_barrier_persists_before_the_patient_is_told(self) -> None:
        """The Coordinator has no promise to withdraw: a write that throws
        never reaches `_say`, so nothing false is said and the turn falls back
        to the fixed ladder the way every other failure in that file does."""
        branch = self._branch(self.coordinator,
                              "if barrier in turn.policy.escalate_only():",
                              'elif barrier in ("asymptomatic"')
        self.assertLess(branch.index("_escalate("), branch.index('"cost_told"'))

    def test_every_patient_facing_escalation_uses_the_one_helper(self) -> None:
        """One helper, so no branch can be given a promise and no fallback."""
        for where, text in (("concierge.py", self.concierge),
                            ("extractor.py", self.extractor)):
            self.assertIn("escalate.told_or_fail_closed", text, where)

    def test_the_fail_closed_emergency_line_still_sends_him_to_hospital(self) -> None:
        """The finding stands. Only the sentence about the doctor is dropped."""
        from core import escalate

        english = escalate.fail_closed_text("en", "u", emergency=True)
        self.assertIn("emergency room", english)
        self.assertNotIn("alerted", english)
        for who in ("m", "f", "u"):
            arabic = escalate.fail_closed_text("ar", who, emergency=True)
            self.assertIn("123", arabic)
            self.assertNotIn("دكتورك اتبلغ", arabic)

    def test_the_fail_closed_relay_line_never_claims_the_doctor_knows(self) -> None:
        from core import escalate

        for speak, who in (("en", "u"), ("ar", "m"), ("ar", "f"), ("ar", "u")):
            line = escalate.fail_closed_text(speak, who, emergency=False)
            self.assertTrue(line.strip())
            self.assertNotIn("اتبلغ", line)
            self.assertNotIn("alerted", line)


# --------------------------------------------------------------------------- #
# Item 16: what the Registrar refuses, and what it says is missing
# --------------------------------------------------------------------------- #
@unittest.skipIf(EXTRACTOR_MISSING, EXTRACTOR_MISSING)
class WhatTheRegistrarWillNotTakeOnTrust(unittest.TestCase):
    """codex item 16. Two different answers to two different problems.

    A field the model could only have got wrong is refused and the doctor is
    asked to restate it. A field the doctor simply did not say is named on the
    card and never filled in, because the dose is the most dangerous field in
    this system and a plausible invented one would pass every gate after it.
    """

    def record(self, **kwargs):
        from core.models import ProposedLoop, ProposedPatient, ProposedRecord

        loops = kwargs.pop("loops", [])
        patient = {"name": "Ahmed Ali", "diagnosis": "heart failure"}
        patient.update(kwargs.pop("patient", {}))
        return ProposedRecord(
            patient=ProposedPatient(**patient),
            plan_text=kwargs.pop("plan_text", "Take one tablet at night."),
            loops=[ProposedLoop(**l) for l in loops],
        )

    def loop(self, **kwargs):
        base = {"type": "TEST", "title": "Lipid panel", "test_name": "Lipid panel"}
        base.update(kwargs)
        return base

    def problems(self, record) -> str:
        from core import registrar

        return "; ".join(registrar.validate(record))

    # --- the outer bounds -------------------------------------------------
    def test_a_due_date_in_the_past_is_refused(self) -> None:
        said = self.problems(self.record(loops=[self.loop(due_in_days=-3)]))
        self.assertIn("in the past", said)

    def test_today_is_allowed(self) -> None:
        self.assertEqual(
            self.problems(self.record(loops=[self.loop(due_in_days=0)])), "")

    def test_a_year_is_allowed_and_a_day_more_is_not(self) -> None:
        self.assertEqual(
            self.problems(self.record(loops=[self.loop(due_in_days=365)])), "")
        self.assertIn("more than 365",
                      self.problems(self.record(loops=[self.loop(due_in_days=366)])))

    def test_an_age_nobody_has_is_refused(self) -> None:
        for age in (-1, 400):
            self.assertIn("not a person's age",
                          self.problems(self.record(patient={"age": age})), age)

    def test_a_real_age_and_no_age_at_all_are_both_fine(self) -> None:
        self.assertEqual(self.problems(self.record(patient={"age": 58})), "")
        self.assertEqual(self.problems(self.record()), "")
        self.assertEqual(self.problems(self.record(patient={"age": 0})), "")
        self.assertEqual(self.problems(self.record(patient={"age": 120})), "")

    def test_a_new_patient_with_no_plan_is_refused(self) -> None:
        """plan_text is the only text ever quoted back to the patient.

        A new record without one opens that person's own page blank under his
        doctor's name.
        """
        from core import registrar

        self.assertIn("no plan", "; ".join(registrar.validate(
            self.record(plan_text="   "), existing=False)))

    def test_an_addition_to_an_existing_record_needs_no_plan(self) -> None:
        """Wave C round 2, Fable's review.

        "Follow up with Ahmed about his potassium in a week" carries no plan
        text and must not be refused: Ahmed's plan is already on his record. An
        empty one appends nothing; a present one becomes the dated addendum S9
        already writes. The end-to-end proof, through `registrar.handle_doctor`
        with a board and an identification, is in
        tests/test_identify.py: `TheRegistrarAgainstABoard`.
        """
        from core import registrar

        self.assertEqual(
            registrar.validate(self.record(plan_text=""), existing=True), [])

    def test_the_plan_check_waits_for_the_identification(self) -> None:
        """None means "not decided yet", and this is the only check that waits.

        `registrar.dictate` runs the structural checks before it spends a model
        call on the identification and asks again afterwards with the answer.
        """
        from core import registrar

        self.assertEqual(registrar.validate(self.record(plan_text="")), [])

    def test_a_loop_with_no_title_is_refused(self) -> None:
        self.assertIn("no title",
                      self.problems(self.record(loops=[self.loop(title=" ")])))

    def test_a_monitor_with_nothing_to_measure_is_refused(self) -> None:
        said = self.problems(self.record(loops=[
            {"type": "MONITOR", "title": "Blood pressure"}]))
        self.assertIn("nothing to measure", said)

    def test_a_task_with_nothing_in_it_is_refused(self) -> None:
        said = self.problems(self.record(loops=[{"type": "TASK", "title": "Chore"}]))
        self.assertIn("nothing in it", said)

    def test_the_checks_that_were_already_there_still_hold(self) -> None:
        self.assertIn("name is missing",
                      self.problems(self.record(patient={"name": "Unknown"})))
        self.assertIn("no test name", self.problems(self.record(
            loops=[{"type": "TEST", "title": "Lipid panel"}])))
        self.assertIn("no drug or no action", self.problems(self.record(
            loops=[{"type": "MEDICATION", "title": "Atorvastatin"}])))

    # --- what is missing is said, never invented --------------------------
    def test_a_medication_start_with_no_dose_is_flagged_and_not_refused(self) -> None:
        from core import registrar

        record = self.record(loops=[{"type": "MEDICATION",
                                     "title": "Atorvastatin", "drug": "atorvastatin",
                                     "action": "start"}])
        self.assertEqual(registrar.validate(record), [],
                         "a real drug for a real patient is not thrown away")
        self.assertIn(registrar.DOSE_MISSING, "; ".join(registrar.flags(record)))

    def test_a_medication_stop_needs_no_dose(self) -> None:
        from core import registrar

        record = self.record(loops=[{"type": "MEDICATION", "title": "Atorvastatin",
                                     "drug": "atorvastatin", "action": "stop"}])
        self.assertEqual(registrar.flags(record), [])

    def test_a_dose_that_was_dictated_is_not_flagged(self) -> None:
        from core import registrar

        record = self.record(loops=[{"type": "MEDICATION", "title": "Atorvastatin",
                                     "drug": "atorvastatin", "action": "start",
                                     "dose": "40 mg at night"}])
        self.assertEqual(registrar.flags(record), [])

    def test_a_monitor_with_no_schedule_or_no_duration_says_so(self) -> None:
        from core import registrar

        record = self.record(loops=[{"type": "MONITOR", "title": "Blood pressure",
                                     "metric": "BP"}])
        said = "; ".join(registrar.flags(record))
        self.assertIn(registrar.SCHEDULE_MISSING, said)
        self.assertIn(registrar.DURATION_MISSING, said)
        self.assertEqual(registrar.validate(record), [])

    def test_the_card_prints_what_is_missing_above_the_safety_sentence(self) -> None:
        from core import contract, registrar

        record = self.record(loops=[{"type": "MEDICATION", "title": "Atorvastatin",
                                     "drug": "atorvastatin", "action": "start"}])
        card = registrar.confirm_card(record, "c1", "Dr Mohamed")
        body = card["lines"]
        self.assertTrue(any(registrar.DOSE_MISSING in line for line in body))
        missing_at = [i for i, line in enumerate(body)
                      if registrar.DOSE_MISSING in line][0]
        safety_at = body.index(contract.SAFETY_SENTENCE)
        self.assertLess(missing_at, safety_at)

    def test_a_card_with_nothing_missing_says_nothing(self) -> None:
        """S17 gave this loop its due date, because a deadline is now missable.

        A TEST loop with no `due_in_days` is a loop with a missing deadline
        since S17, and the block says so, so "nothing missing" has to mean the
        date as well. The absence itself is proved in tests/test_due_dates.py.
        """
        from core import registrar

        card = registrar.confirm_card(
            self.record(loops=[self.loop(due_in_days=14)]), "c1", "Dr Mohamed")
        self.assertNotIn("Not dictated", " ".join(card["lines"]))


# --------------------------------------------------------------------------- #
# The two pages
# --------------------------------------------------------------------------- #
class ThePatientPageKeepsWhatItCouldNotSend(unittest.TestCase):
    """codex item 18 and the Codex web report W1 and W2."""

    def setUp(self) -> None:
        self.page = (APP_ROOT / "web" / "patient.html").read_text("utf-8")

    def test_the_send_checks_the_response(self) -> None:
        self.assertIn("if (!r.ok)", self.page)

    def test_the_text_and_the_file_are_only_cleared_on_success(self) -> None:
        send = self.page.split("async function send()", 1)[1].split(
            "el(\"send\").onclick", 1)[0]
        self.assertLess(send.index("if (!r.ok)"), send.index('el("t").value = ""'))

    def test_a_failed_send_shows_a_retry_line(self) -> None:
        self.assertIn("retry", self.page.lower())

    def test_the_poll_has_a_catch(self) -> None:
        poll = self.page.split("async function poll()", 1)[1].split(
            "async function send()", 1)[0]
        self.assertIn("catch", poll)

    def test_a_failed_poll_says_reconnecting(self) -> None:
        self.assertIn("reconnect", self.page.lower())


class TheDashboardSaysWhatItIsAndWhetherItIsLive(unittest.TestCase):
    """Codex web report W3 and W4, and codex items 21 and 22."""

    def setUp(self) -> None:
        self.raw = (APP_ROOT / "web" / "dashboard.html").read_bytes()
        self.page = self.raw.decode("utf-8")

    def test_there_is_no_nul_byte_in_the_file(self) -> None:
        self.assertEqual(self.raw.count(b"\x00"), 0)

    def test_every_nav_button_carries_its_full_name(self) -> None:
        nav = self.page.split('<nav class="main"', 1)[1].split("</nav>", 1)[0]
        for name in ("Board", "Patients", "Inbox", "Reports", "Settings"):
            self.assertIn(f'aria-label="{name}"', nav, name)

    def test_the_inbox_count_is_announced_as_a_sentence(self) -> None:
        self.assertIn('"Inbox, " + n + " open"', self.page)

    def test_the_pill_has_an_amber_and_a_grey_state(self) -> None:
        self.assertIn("Reconnecting", self.page)
        self.assertIn("Offline since", self.page)

    def test_a_failed_poll_counts_and_a_good_one_clears_it(self) -> None:
        poll = self.page.split("async function poll()", 1)[1].split(
            "5. Demo panel", 1)[0]
        self.assertIn("S.failedPolls = 0", poll)
        self.assertIn("S.failedPolls++", poll)

    def test_the_dictation_sheet_takes_the_newest_confirmation(self) -> None:
        """codex item 21. /cards answers newest first, so that is pending[0]."""
        block = self.page.split("function showConfirmInSheet(", 1)[1].split(
            "\n}", 1)[0]
        self.assertIn("pending[0]", block)
        self.assertNotIn("pending[pending.length - 1]", block)


# --------------------------------------------------------------------------- #
# The Dictate button and the runbook say the same sentence (S15 G3)
# --------------------------------------------------------------------------- #
# The demo panel's first button is the sentence Mohamed dictates on camera. It
# used to be a shorter LDL line that opened one loop, while docs/RUNBOOK.md
# section 1b dictated four: a rehearsal from the button and a take from the
# document were not the same take. There is no reason for two copies of one
# sentence to exist and no way to notice by eye when one of them moves, so the
# rail is here: the runbook block is the source, both pages quote it, and this
# test fails the build the moment either drifts.
RUNBOOK_PATH = APP_ROOT.parent / "docs" / "RUNBOOK.md"
# The image's build context is `app/` alone, so docs/RUNBOOK.md is not in it.
# Same reasoning as tests/test_background.py: skip only where the file cannot
# exist, which is never in a checkout of the whole tree.
HAS_RUNBOOK = unittest.skipUnless(
    RUNBOOK_PATH.exists(), "docs/RUNBOOK.md is outside the image")

_JS_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


def runbook_beat_one() -> str:
    """The RUNBOOK 1b "Beat 1, the exact dictation" block, as one line."""
    body = RUNBOOK_PATH.read_text(encoding="utf-8").split(
        "**Beat 1, the exact dictation:**", 1)[1]
    quoted: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if quoted:
                break
            continue
        if not stripped.startswith(">"):
            break
        quoted.append(stripped.lstrip(">").strip())
    return " ".join(" ".join(quoted).split())


def dictate_button(page: str) -> str:
    """The text the "1. Dictate" demo button puts in the doctor's box.

    Read out of the BEATS entry rather than off a single line, because the
    string is wrapped with `+` across three source lines in both pages and a
    rail that only matches one layout is a rail that breaks on reformatting.
    """
    entry = page.split('["1. Dictate", "doctor",', 1)[1].split("],", 1)[0]
    return "".join(_JS_STRING.findall(entry))


class TheDictateButtonQuotesTheRunbook(unittest.TestCase):
    @HAS_RUNBOOK
    def test_the_dashboard_button_is_the_runbook_sentence(self) -> None:
        page = (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(dictate_button(page), runbook_beat_one())

    @HAS_RUNBOOK
    def test_the_console_button_is_the_runbook_sentence(self) -> None:
        page = (APP_ROOT / "web" / "console.html").read_text(encoding="utf-8")
        self.assertEqual(dictate_button(page), runbook_beat_one())

    def test_both_pages_carry_the_same_sentence(self) -> None:
        """This half needs no document, so it runs inside the image too."""
        dashboard = (APP_ROOT / "web" / "dashboard.html").read_text(
            encoding="utf-8")
        console = (APP_ROOT / "web" / "console.html").read_text(encoding="utf-8")
        self.assertEqual(dictate_button(dashboard), dictate_button(console))

    def test_the_sentence_opens_all_four_of_beat_ones_loops(self) -> None:
        """The point of the change: four obligations, not one.

        A button that opened a single lipid loop could not rehearse the beat the
        spine films, so the words each loop is recognised by are asserted here
        and not left to the reader to spot.
        """
        said = dictate_button(
            (APP_ROOT / "web" / "dashboard.html").read_text(encoding="utf-8"))
        for phrase in ("Ahmed Ali, 58, male", "Start atorvastatin 40 at night",
                       "Lipid panel in 2 weeks",
                       "Blood pressure twice a day for 7 days",
                       "Come back in 3 weeks"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, said)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
