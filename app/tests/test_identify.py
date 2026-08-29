"""S9: which patient a dictation is about, and what happens when it is not new.

Two halves, for the reason tests/test_dashboard_routes.py has three.

The first half drives `core/identify.py`, which is pure: the two code patterns,
the board rows, the name matcher applied to them, and the rule that reads a
model verdict and decides what the doctor is shown. It runs anywhere with
nothing installed, because that rule is the whole safety argument of this
feature: the model never chooses a record, it only offers a reading, and code
decides between offering and asking.

The second half imports the Registrar and the action route and drives them
against an in-memory store, so it reaches FastAPI and the cloud SDK and skips on
a laptop with neither. Every model call in it is a fixture: `registrar.propose`
returns a record and `identify.identify` returns a verdict, so nothing here asks
Gemini anything and the tests are about the code around it.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from pathlib import Path

from core import identify

from tests import Borrowable
from core.identify import Candidate, Verdict

APP_ROOT = Path(__file__).resolve().parents[1]
CORE = APP_ROOT / "core"
MAIN = (APP_ROOT / "main.py").read_text(encoding="utf-8")

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


def row(ident: str, name: str, age=None, diagnosis: str = "",
        notes: tuple = (), last: str = "2026-08-01") -> identify.BoardRow:
    return identify.BoardRow(id=ident, name=name, age=age, diagnosis=diagnosis,
                             notes=notes, last_seen=last)


AHMED = row("p1", "Ahmed Ali", 58, "heart failure")
HEND = row("p2", "Hend Ismail", 44, "hypothyroidism")
AHMED_TWO = row("p3", "Ahmed Saleh", 61, "type 2 diabetes")
SALAH = row("p4", "Salah Mahmoud", 70, "atrial fibrillation",
            notes=("father of Dr Tarek",))
BOARD = [AHMED, HEND, AHMED_TWO, SALAH]


def verdict(intent: str, *candidates, note: str = "") -> Verdict:
    return Verdict(intent=intent, note=note, candidates=[
        Candidate(patient_id=pid, confidence=conf, reason=why)
        for pid, conf, why in candidates
    ])


# --------------------------------------------------------------------------- #
# 1. The rules, which run anywhere
# --------------------------------------------------------------------------- #
class TheTwoThingsCodeObeysWhateverTheModelSaid(unittest.TestCase):
    def test_an_explicit_new_patient_is_read_in_both_languages(self) -> None:
        for said in ("This is a new patient, Ahmed Ali, 58",
                     "new case: Hend, 44",
                     "مريض جديد اسمه أحمد علي",
                     "دي حالة جديدة، هند، ٤٤ سنة",
                     "أول مرة يجيلي، سلاح محمود"):
            with self.subTest(said=said):
                self.assertTrue(identify.says_new(said))

    def test_an_ordinary_dictation_is_not_read_as_one(self) -> None:
        for said in ("follow up with Ahmed about his potassium in a week",
                     "remind Ahmed to send the lipid panel",
                     "هند تعمل تحليل الغدة بعد أسبوعين",
                     ""):
            with self.subTest(said=said):
                self.assertFalse(identify.says_new(said))

    def test_a_lookup_is_read_in_both_languages(self) -> None:
        for said in ("look for a patient named Ahmed",
                     "search for Hend",
                     "دور على مريض اسمه أحمد",
                     "هاتلي هند إسماعيل"):
            with self.subTest(said=said):
                self.assertTrue(identify.asks_lookup(said))

    def test_an_ordinary_dictation_is_not_read_as_a_lookup(self) -> None:
        for said in ("follow up with Ahmed about his potassium in a week",
                     "new patient, Ahmed Ali, 58, heart failure"):
            with self.subTest(said=said):
                self.assertFalse(identify.asks_lookup(said))


class TheBoardTheModelIsAllowedToSee(unittest.TestCase):
    def patients(self, count: int) -> list:
        return [SimpleNamespace(id=f"p{i}", name=f"Patient {i}", age=40 + i,
                                sex="male", diagnosis="something",
                                notes=[{"text": f"note {i}", "at": "2026-08-01"}],
                                created_at=NOW - timedelta(days=count - i))
                for i in range(count)]

    def test_it_is_bounded_to_fifty_and_keeps_the_most_recent(self) -> None:
        people = self.patients(70)
        seen = {p.id: NOW - timedelta(days=70 - i)
                for i, p in enumerate(people)}
        rows = identify.board(people, seen)
        self.assertEqual(len(rows), identify.BOARD_LIMIT)
        self.assertNotIn("p0", [r.id for r in rows])
        self.assertIn("p69", [r.id for r in rows])

    def test_the_rows_come_back_in_board_order(self) -> None:
        people = self.patients(4)
        rows = identify.board(people, {})
        self.assertEqual([r.id for r in rows], ["p0", "p1", "p2", "p3"])

    def test_a_record_with_no_notes_field_at_all_does_not_break_it(self) -> None:
        bare = SimpleNamespace(id="p9", name="Nobody", age=None, sex="",
                               diagnosis="", created_at=NOW)
        rows = identify.board([bare], {})
        self.assertEqual(rows[0].notes, ())

    def test_the_notes_reach_the_context_the_model_reads(self) -> None:
        block = identify.context_block([SALAH])
        self.assertIn("father of Dr Tarek", block)
        self.assertIn("id=p4", block)

    def test_a_row_says_the_name_the_age_and_the_diagnosis(self) -> None:
        self.assertEqual(AHMED.label(), "Ahmed Ali, 58, heart failure")


class TheNameMatcherAppliedToTheBoard(unittest.TestCase):
    def test_a_first_name_two_patients_share_matches_both(self) -> None:
        found = identify.code_matches(BOARD, "Ahmed")
        self.assertEqual([r.id for r in found], ["p1", "p3"])

    def test_a_whole_name_is_that_one_patient(self) -> None:
        self.assertEqual([r.id for r in identify.code_matches(BOARD, "Ahmed Ali")],
                         ["p1"])

    def test_two_records_with_the_same_written_name_are_both_returned(self) -> None:
        twins = [row("a", "Ahmed Ali", 58), row("b", "Ahmed Ali", 31)]
        self.assertEqual([r.id for r in identify.code_matches(twins, "Ahmed Ali")],
                         ["a", "b"])

    def test_nobody_is_nobody(self) -> None:
        self.assertEqual(identify.code_matches(BOARD, "Mariam Fouad"), [])


class TheRuleOverTheVerdict(unittest.TestCase):
    """The whole of S9's safety argument, as one table of cases."""

    def decide(self, said, name, said_verdict, board=None):
        return identify.decide(said, name, board or BOARD, said_verdict)

    def test_no_match_at_all_is_a_new_patient(self) -> None:
        out = self.decide("Mariam Fouad, 33, anaemia", "Mariam Fouad",
                          verdict("new_patient"))
        self.assertEqual(out.kind, identify.NEW)
        self.assertEqual(out.patient_id, "")

    def test_one_match_the_model_agrees_with_is_the_only_auto_selection(
            self) -> None:
        out = self.decide("follow up with Ahmed Ali about his potassium",
                          "Ahmed Ali",
                          verdict("existing_patient",
                                  ("p1", 0.95, "'Ahmed Ali' is on the board")))
        self.assertEqual(out.kind, identify.EXISTING)
        self.assertEqual(out.patient_id, "p1")
        self.assertIn("the model agrees", out.why)

    def test_two_matches_ask_and_choose_nothing(self) -> None:
        out = self.decide("follow up with Ahmed about his potassium", "Ahmed",
                          verdict("existing_patient",
                                  ("p1", 0.6, "shares the first name"),
                                  ("p3", 0.6, "shares the first name")))
        self.assertEqual(out.kind, identify.ASK)
        self.assertEqual(out.patient_id, "")
        self.assertEqual(sorted(out.ids()), ["p1", "p3"])

    def test_a_description_only_match_never_auto_selects(self) -> None:
        """No name in the dictation at all, so the name matcher has nothing."""
        out = self.decide("the father of my friend Tarek needs a follow up visit",
                          "Tarek's father",
                          verdict("existing_patient",
                                  ("p4", 0.99, "'father of your friend Tarek' "
                                               "matched the note on Salah Mahmoud")))
        self.assertEqual(out.kind, identify.ASK)
        self.assertEqual(out.ids(), ("p4",))
        self.assertIn("father of your friend Tarek", dict(out.candidates)["p4"])

    def test_a_confidence_below_the_threshold_asks(self) -> None:
        out = self.decide("follow up with Ahmed Ali", "Ahmed Ali",
                          verdict("existing_patient", ("p1", 0.4, "might be him")))
        self.assertEqual(out.kind, identify.ASK)

    def test_an_explicit_new_patient_beats_a_model_that_says_existing(
            self) -> None:
        out = self.decide("this is a new patient, Ahmed Ali, 40, asthma",
                          "Ahmed Ali",
                          verdict("existing_patient",
                                  ("p1", 0.99, "the name is on the board")))
        self.assertEqual(out.kind, identify.NEW)
        self.assertIn("the doctor said", out.why)

    def test_lookup_lists_and_decides_nothing(self) -> None:
        out = self.decide("look for a patient named Ahmed", "Ahmed",
                          verdict("lookup", ("p1", 0.9, "name"),
                                  ("p3", 0.9, "name")))
        self.assertEqual(out.kind, identify.LIST)
        self.assertEqual(sorted(out.ids()), ["p1", "p3"])

    def test_a_lookup_the_code_pattern_sees_is_a_lookup_whatever_the_model_says(
            self) -> None:
        out = self.decide("look for a patient named Ahmed Ali", "Ahmed Ali",
                          verdict("existing_patient", ("p1", 0.99, "name")))
        self.assertEqual(out.kind, identify.LIST)

    def test_unclear_asks_for_the_name(self) -> None:
        out = self.decide("the one from last week", "the patient",
                          verdict("unclear"))
        self.assertEqual(out.kind, identify.ASK)
        self.assertTrue(out.needs_name)
        self.assertEqual(out.candidates, ())

    def test_a_model_error_falls_back_to_the_matcher_and_still_asks(self) -> None:
        out = self.decide("follow up with Ahmed Ali about his potassium",
                          "Ahmed Ali", None)
        self.assertEqual(out.kind, identify.ASK)
        self.assertEqual(out.ids(), ("p1",))
        self.assertIn("model was unavailable", out.why)

    def test_a_model_error_with_nothing_to_ask_about_still_asks(self) -> None:
        """codex re-audit 1. This was NEW, and NEW was a second Ahmed.

        The model is the half that reads a dictation with no name in it, so a
        board that already has patients on it and a model that could not be
        reached is a question and not an answer.
        """
        out = self.decide("Mariam Fouad, 33, anaemia", "Mariam Fouad", None)
        self.assertEqual(out.kind, identify.ASK)
        self.assertTrue(out.needs_name)
        self.assertEqual(out.ids(), ())
        self.assertIn("model was unavailable", out.why)

    def test_the_doctor_saying_new_patient_still_wins_over_the_outage(
            self) -> None:
        """The one sentence that always creates a record, model or no model."""
        out = self.decide("new patient Mariam Fouad, 33, anaemia",
                          "Mariam Fouad", None)
        self.assertEqual(out.kind, identify.NEW)

    def test_an_empty_board_is_a_new_patient_and_costs_no_model_call(
            self) -> None:
        out = identify.decide("Ahmed Ali, 58, heart failure", "Ahmed Ali", [],
                              None)
        self.assertEqual(out.kind, identify.NEW)
        self.assertIn("no patients on the board yet", out.why)

    def test_the_note_survives_into_the_outcome(self) -> None:
        out = self.decide("new patient, Salah's son, 30, asthma", "Salah's son",
                          verdict("new_patient", note="son of Salah Mahmoud"))
        self.assertEqual(out.note, "son of Salah Mahmoud")

    def test_a_candidate_the_board_does_not_carry_is_dropped(self) -> None:
        """The model may not invent an id, and if it does nothing happens."""
        out = self.decide("Mariam Fouad, 33, anaemia", "Mariam Fouad",
                          verdict("existing_patient", ("ghost", 0.99, "made up")))
        self.assertEqual(out.kind, identify.NEW)


class TheNotesAreDoctorTextAndStayThere(unittest.TestCase):
    """A note says who somebody is. The patient must never be shown one.

    "father of Dr Tarek" is the doctor's own shorthand about a person, written
    for his own recall. It is on the record because a later description has to
    find the right patient, and it has no business anywhere near the chat the
    patient reads. This is a rail over the source rather than a hope: the one
    route a patient can reach serves three fields, and no module on the patient
    path reads the field at all.
    """

    def test_the_patient_feed_serves_only_the_kind_the_text_and_the_time(
            self) -> None:
        feed = MAIN.split("async def patient_feed(", 1)[1].split(
            '@app.post("/p/', 1)[0]
        self.assertIn('"kind": e.kind, "text": e.text', feed)
        self.assertNotIn("notes", feed)

    def test_no_module_on_the_patient_path_reads_the_notes(self) -> None:
        """`patient.notes`, the field itself. core/coordinator.py has a
        `decision.notes` of its own and it is a different thing entirely."""
        for name in ("concierge.py", "coordinator.py", "templates.py",
                     "validator.py", "links.py", "chaser.py", "intents.py",
                     "contract.py", "sentinel.py"):
            with self.subTest(module=name):
                self.assertNotIn("patient.notes",
                                 (CORE / name).read_text(encoding="utf-8"))

    def test_the_only_two_places_that_touch_the_field_are_the_two_intended(
            self) -> None:
        writers = sorted(path.name for path in CORE.glob("*.py")
                         if "patient.notes" in path.read_text(encoding="utf-8"))
        self.assertEqual(writers, ["registrar.py"])
        self.assertIn('"notes": patient.notes', MAIN)

    def test_only_the_registrar_writes_one(self) -> None:
        registrar_source = (CORE / "registrar.py").read_text(encoding="utf-8")
        self.assertIn("def note_entries(", registrar_source)
        self.assertIn('"source": "doctor dictation"', registrar_source)


def _boom(rows):  # noqa: ARG001 - it exists to raise
    raise RuntimeError("the model is not reachable")


class TheIdentificationFailsClosed(unittest.IsolatedAsyncioTestCase):
    """Anything at all going wrong is None, and None is the ask card.

    This runs on a laptop with no SDK too, where the import inside the call is
    what fails. In the image it is `context_block` that raises. Both are the
    same promise: this function does not have a path that throws.
    """

    async def test_it_returns_none_rather_than_raising(self) -> None:
        with patch.object(identify, "context_block", _boom):
            self.assertIsNone(await identify.identify("anything", BOARD))

    async def test_an_empty_dictation_is_not_asked_about_at_all(self) -> None:
        self.assertIsNone(await identify.identify("", []))

    async def test_an_empty_board_is_never_asked_about(self) -> None:
        """No call at all: `context_block` would raise if one were made."""
        with patch.object(identify, "context_block", _boom):
            self.assertIsNone(await identify.identify("Ahmed Ali, 58", []))


# --------------------------------------------------------------------------- #
# 2. The Registrar and the action route, against an in-memory store
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - the image build always has these
    import main as sanad_main
    from core import chaser, concierge, events as events_module, links
    from core import registrar, store as store_module, telegram
    from core.models import (Doctor, Event, Loop, Patient, PendingConfirm,
                             ProposedRecord)
    SDK_MISSING = ""
except Exception as exc:  # pragma: no cover
    SDK_MISSING = f"the Registrar is not importable here: {exc}"


NEW_RECORD = {
    "patient": {"name": "Mariam Fouad", "age": 33, "sex": "female",
                "diagnosis": "iron deficiency anaemia"},
    "baseline": [{"name": "Hb", "value": "9.1"}],
    "targets": [],
    "plan_text": "Start iron tablets and repeat the blood count in a month.",
    "loops": [{"type": "TEST", "title": "Complete blood count",
               "test_name": "CBC", "due_in_days": 30}],
}

POTASSIUM = {
    "patient": {"name": "Ahmed", "age": None, "sex": None, "diagnosis": ""},
    "baseline": [], "targets": [],
    "plan_text": "We will check your potassium again in a week.",
    "loops": [{"type": "TEST", "title": "Serum potassium",
               "test_name": "Potassium", "due_in_days": 7}],
}

LIPID = {
    "patient": {"name": "Ahmed", "age": None, "sex": None, "diagnosis": ""},
    "baseline": [], "targets": [],
    "plan_text": "Please send the lipid panel result when you have it.",
    "loops": [{"type": "TASK", "title": "Send the lipid panel",
               "text": "send the result"}],
}


class FakeStore:
    """Just enough Firestore for an intake, in memory.

    Every read hands back a fresh object, so "the record changed" can never be
    an object the test itself mutated.
    """

    def __init__(self) -> None:
        self.doctors: dict = {}
        self.patients: dict = {}
        self.loops: dict = {}
        self.events: dict = {}
        self.confirms: dict = {}
        self.actions: set = set()
        self.clock = NOW
        self.counter = 0

    def now(self):
        return self.clock

    def new_id(self) -> str:
        self.counter += 1
        return f"id{self.counter}"

    async def doctor_by_token(self, token: str):
        for doctor in self.doctors.values():
            if doctor.web_token == token:
                return Doctor(**doctor.model_dump())
        return None

    async def doctor_by_id(self, doctor_id: str):
        row = self.doctors.get(doctor_id)
        return Doctor(**row.model_dump()) if row else None

    async def create_patient(self, patient: Patient) -> Patient:
        self.patients[patient.id] = patient
        return patient

    async def get_patient(self, patient_id: str):
        row = self.patients.get(patient_id)
        return Patient(**row.model_dump()) if row else None

    async def list_patients(self, doctor_id: str) -> list:
        rows = [Patient(**p.model_dump()) for p in self.patients.values()
                if p.doctor_id == doctor_id]
        return sorted(rows, key=lambda p: p.created_at)

    async def update_patient(self, patient_id: str, **fields) -> None:
        row = self.patients[patient_id]
        self.patients[patient_id] = Patient(**{**row.model_dump(), **fields})

    async def create_loop(self, loop: Loop) -> Loop:
        self.loops[loop.id] = loop
        return loop

    async def list_loops(self, patient_id: str) -> list:
        rows = [Loop(**l.model_dump()) for l in self.loops.values()
                if l.patient_id == patient_id]
        return sorted(rows, key=lambda l: l.created_at)

    async def add_event(self, event: Event) -> Event:
        self.events[event.id] = event
        return event

    async def list_events(self, doctor_id: str) -> list:
        rows = [Event(**e.model_dump()) for e in self.events.values()
                if e.doctor_id == doctor_id]
        return sorted(rows, key=lambda e: e.ts)

    async def update_event(self, event_id: str, **fields) -> None:
        row = self.events[event_id]
        self.events[event_id] = Event(**{**row.model_dump(), **fields})

    async def claim_action(self, doctor_id: str, action_id: str) -> bool:
        """The domain-work key behind a card (codex re-audit 17)."""
        key = f"{doctor_id}:{action_id}"
        if key in self.actions:
            return False
        self.actions.add(key)
        return True

    async def release_action(self, doctor_id: str, action_id: str) -> None:
        self.actions.discard(f"{doctor_id}:{action_id}")

    async def claim_card_action(self, event_id: str, action_id: str, at) -> bool:
        """The card-action claim (codex item 17), as the transaction behaves."""
        row = self.events.get(event_id)
        if row is None:
            return False
        meta = dict(row.meta or {})
        card = dict(meta.get("card") or {})
        if card.get("claimed_by") or card.get("resolved"):
            return False
        card["claimed_by"] = action_id
        card["claimed_at"] = at.isoformat()
        meta["card"] = card
        await self.update_event(event_id, meta=meta)
        return True

    async def release_card_action(self, event_id: str) -> None:
        row = self.events.get(event_id)
        if row is None:
            return
        meta = dict(row.meta or {})
        card = dict(meta.get("card") or {})
        card.pop("claimed_by", None)
        card.pop("claimed_at", None)
        meta["card"] = card
        await self.update_event(event_id, meta=meta)

    async def save_confirm(self, confirm: PendingConfirm) -> PendingConfirm:
        self.confirms[confirm.id] = confirm
        return confirm

    async def get_confirm(self, confirm_id: str):
        row = self.confirms.get(confirm_id)
        return PendingConfirm(**row.model_dump()) if row else None

    async def delete_confirm(self, confirm_id: str) -> None:
        self.confirms.pop(confirm_id, None)

    async def claim_confirm(self, confirm_id: str) -> bool:
        """The commit claim (codex item 6), as the transaction behaves."""
        confirm = self.confirms.get(confirm_id)
        if confirm is None or confirm.state != "pending":
            return False
        confirm.state = "committing"
        return True

    async def release_confirm(self, confirm_id: str) -> None:
        confirm = self.confirms.get(confirm_id)
        if confirm is not None:
            confirm.state = "pending"

    async def list_link_tokens(self, doctor_id: str) -> list:
        return []


STORE_NAMES = ("now", "new_id", "doctor_by_token", "doctor_by_id",
               "create_patient", "get_patient", "list_patients",
               "update_patient", "create_loop", "list_loops", "add_event",
               "list_events", "update_event", "save_confirm", "get_confirm",
               "delete_confirm", "list_link_tokens", "claim_confirm",
               "release_confirm", "claim_card_action", "release_card_action",
               "claim_action", "release_action")


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheRegistrarAgainstABoard(Borrowable):
    def setUp(self) -> None:
        outer = self
        self.fake = FakeStore()
        self.sent: list = []
        self.minted: list = []
        self.scheduled: list = []
        self.verdict = verdict("new_patient")
        self.record = NEW_RECORD

        self.doctor = Doctor(id="d", name="Dr Mohamed", specialty="cardiology",
                             lang="en", web_token="goodtoken",
                             telegram_chat_id=None, created_at=NOW)
        self.fake.doctors["d"] = self.doctor

        class Fanout:
            """The web half of core/adapters.py: a send is an event."""

            async def send(self, ref, msg):
                outer.sent.append((ref, msg))
                meta = dict(msg.meta or {})
                if msg.card:
                    meta["card"] = msg.card
                written = await events_module.append_event(
                    outer.doctor.id, "card" if msg.card else "agent_out",
                    msg.text, patient_id=msg.patient_id, meta=meta)
                return written.id

        async def propose(text):
            return ProposedRecord.model_validate(outer.record)

        async def identify_stub(text, rows, extracted_name=""):
            outer.board_seen = list(rows)
            return outer.verdict

        async def mint(doctor, patient, token_id=""):
            outer.minted.append(patient.id)
            return SimpleNamespace(id="tok1", patient_id=patient.id)

        async def card_lines(token, base_url=""):
            return ["link: https://example/p/tok1"]

        async def schedule_loop(loop):
            outer.scheduled.append(loop.id)
            return [{"loop": loop.id}]

        async def schedule_patient(patient):
            rows = await outer.fake.list_loops(patient.id)
            for loop in rows:
                outer.scheduled.append(loop.id)
            return [{"loop": l.id} for l in rows]

        for name in STORE_NAMES:
            self.enterContext(patch.object(store_module, name,
                                           getattr(self.fake, name)))
        self.enterContext(patch.object(registrar, "fanout", lambda: Fanout()))
        self.enterContext(patch.object(registrar, "propose", propose))
        self.enterContext(patch.object(identify, "identify", identify_stub))
        self.enterContext(patch.object(links, "mint", mint))
        self.enterContext(patch.object(links, "card_lines", card_lines))
        self.enterContext(patch.object(chaser, "schedule_loop", schedule_loop))
        self.enterContext(patch.object(chaser, "schedule_patient",
                                       schedule_patient))
        self.enterContext(patch.object(telegram, "enabled", lambda: False))

    # helpers ---------------------------------------------------------------
    def board(self, *rows: Patient) -> None:
        for one in rows:
            self.fake.patients[one.id] = one

    def ahmed(self) -> Patient:
        return Patient(id="p1", doctor_id="d", name="Ahmed Ali", age=58,
                       sex="male", diagnosis="heart failure",
                       plan_text="Bisoprolol 2.5 mg once a day.",
                       baseline={"LDL": "160"}, created_at=NOW)

    def second_ahmed(self) -> Patient:
        return Patient(id="p3", doctor_id="d", name="Ahmed Saleh", age=61,
                       sex="male", diagnosis="type 2 diabetes",
                       plan_text="Metformin twice a day.", created_at=NOW)

    def cards(self) -> list:
        return [msg.card for _, msg in self.sent if msg.card]

    def last_card(self) -> dict:
        return self.cards()[-1]

    def action_ids(self, card: dict) -> list:
        return [a["id"] for a in card.get("actions", [])]

    async def dictate(self, text: str) -> None:
        await registrar.handle_doctor(self.doctor, text)

    # the four answers ------------------------------------------------------
    async def test_no_match_is_a_new_patient_card(self) -> None:
        self.board(self.ahmed())
        self.record, self.verdict = NEW_RECORD, verdict("new_patient")
        await self.dictate("Mariam Fouad, 33, iron deficiency anaemia, "
                           "repeat the blood count in a month")
        card = self.last_card()
        self.assertEqual(card["title"], "New patient: Mariam Fouad")
        self.assertEqual(self.action_ids(card)[:2],
                         [f"confirm:{self.confirm_id()}",
                          f"cancel:{self.confirm_id()}"])
        self.assertNotIn("newpatient:" + self.confirm_id(),
                         self.action_ids(card))

    async def test_one_match_the_model_agrees_with_is_the_existing_card(
            self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient",
                               ("p1", 0.95, "'Ahmed' is Ahmed Ali on the board"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        card = self.last_card()
        self.assertEqual(card["title"],
                         "Existing patient: Ahmed Ali, 58, heart failure")
        self.assertIn(f"newpatient:{self.confirm_id()}", self.action_ids(card))
        self.assertEqual((await self.fake.get_confirm(self.confirm_id())).patient_id,
                         "p1")

    async def test_two_matches_ask_and_write_nothing(self) -> None:
        self.board(self.ahmed(), self.second_ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient",
                               ("p1", 0.6, "shares the first name"),
                               ("p3", 0.6, "shares the first name"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        card = self.last_card()
        self.assertEqual(card["title"], "Which patient is this?")
        self.assertIn(f"existing:p1:{self.confirm_id()}", self.action_ids(card))
        self.assertIn(f"existing:p3:{self.confirm_id()}", self.action_ids(card))
        self.assertIn(f"newpatient:{self.confirm_id()}", self.action_ids(card))
        self.assertEqual(len(self.fake.patients), 2)
        self.assertEqual(self.fake.loops, {})
        self.assertIsNone((await self.fake.get_confirm(self.confirm_id())).patient_id)

    async def test_a_dictation_with_no_name_asks_for_the_name(self) -> None:
        self.record = {**NEW_RECORD,
                       "patient": {**NEW_RECORD["patient"], "name": "Unknown"}}
        await self.dictate("start iron tablets and repeat the count in a month")
        texts = [msg.text for _, msg in self.sent]
        self.assertTrue(any("the patient's name is missing" in t for t in texts))
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.fake.confirms, {})

    # the follow-up phrasings the brief names --------------------------------
    async def test_follow_up_with_ahmed_about_his_potassium_lands_on_ahmed(
            self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertEqual(len(self.fake.patients), 1)
        loops = await self.fake.list_loops("p1")
        self.assertEqual([l.title for l in loops], ["Serum potassium"])
        self.assertEqual(self.scheduled, [loops[0].id])

    async def test_remind_ahmed_to_send_the_lipid_panel_lands_on_ahmed(
            self) -> None:
        self.board(self.ahmed())
        self.record = LIPID
        self.verdict = verdict("existing_patient", ("p1", 0.9, "Ahmed Ali"))
        await self.dictate("remind Ahmed to send the lipid panel")
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertEqual(len(self.fake.patients), 1)
        loops = await self.fake.list_loops("p1")
        self.assertEqual([l.title for l in loops], ["Send the lipid panel"])

    # the plan grows, it is never replaced -----------------------------------
    async def test_a_follow_up_with_no_plan_of_its_own_still_reaches_a_card(
            self) -> None:
        """Wave C round 2, Fable's review.

        codex item 16 made `plan_text` a hard requirement, and that is right for
        a new patient: the plan is the only text a patient ever reads under his
        doctor's name, and a record arriving without one means his page is
        blank. It is wrong for an addition to a record that already has a plan.
        "Follow up with Ahmed about his potassium in a week" is a whole
        dictation, and the model may return no plan text for it at all, because
        there is no new plan: Ahmed's plan is already on his record. Refusing
        that was refusing the S9 path.
        """
        self.board(self.ahmed())
        self.record = {**POTASSIUM, "plan_text": ""}
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")

        card = self.last_card()
        self.assertEqual(card["title"],
                         "Existing patient: Ahmed Ali, 58, heart failure")
        self.assertNotIn("could not register this safely",
                         " ".join(m.text for _, m in self.sent))
        self.assertEqual(
            (await self.fake.get_confirm(self.confirm_id())).patient_id, "p1")

    async def test_that_follow_up_commits_and_touches_no_plan(self) -> None:
        self.board(self.ahmed())
        self.record = {**POTASSIUM, "plan_text": ""}
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())

        after = await self.fake.get_patient("p1")
        self.assertEqual(after.plan_text, "Bisoprolol 2.5 mg once a day.")
        self.assertNotIn("Addendum", after.plan_text)
        loops = await self.fake.list_loops("p1")
        self.assertEqual([l.title for l in loops], ["Serum potassium"])

    async def test_a_new_patient_with_no_plan_is_still_refused(self) -> None:
        """The other half of the rule, so the loosening is not a hole.

        A new record with no plan is a patient whose own page would open empty
        under his doctor's name. That one is still sent back.
        """
        self.board(self.ahmed())
        self.record = {**NEW_RECORD, "plan_text": "   "}
        self.verdict = verdict("new_patient")
        await self.dictate("Mariam Fouad, 33, iron deficiency anaemia")

        said = " ".join(m.text for _, m in self.sent)
        self.assertIn("could not register this safely", said)
        self.assertIn("no plan", said)
        self.assertEqual(self.fake.confirms, {})

    async def test_the_plan_is_appended_as_a_dated_addendum(self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        plan = (await self.fake.get_patient("p1")).plan_text
        self.assertIn("Bisoprolol 2.5 mg once a day.", plan)
        self.assertIn(concierge.addendum(POTASSIUM["plan_text"], NOW), plan)

    async def test_the_addendum_is_the_same_shape_the_doctors_answers_use(
            self) -> None:
        self.assertEqual(concierge.addendum("do the test", NOW),
                         "[Addendum 2026-08-29] do the test")

    async def test_the_confirm_card_shows_the_addendum_before_the_tap(
            self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        lines = " ".join(self.last_card()["lines"])
        self.assertIn("[Addendum 2026-08-29]", lines)
        self.assertIn("never replaced", lines)

    async def test_only_the_fields_the_dictation_mentioned_are_changed(
            self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        after = await self.fake.get_patient("p1")
        self.assertEqual(after.age, 58)
        self.assertEqual(after.sex, "male")
        self.assertEqual(after.diagnosis, "heart failure")
        self.assertEqual(after.baseline, {"LDL": "160"})

    async def test_a_metric_the_dictation_states_is_added_and_not_a_reset(
            self) -> None:
        self.board(self.ahmed())
        self.record = {**POTASSIUM,
                       "baseline": [{"name": "K", "value": "5.9"}]}
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        after = await self.fake.get_patient("p1")
        self.assertEqual(after.baseline, {"LDL": "160", "K": "5.9"})

    # the buttons ------------------------------------------------------------
    async def test_the_new_patient_button_switches_a_proposal_to_a_new_record(
            self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.choose_new(self.doctor, self.confirm_id())
        card = self.last_card()
        self.assertEqual(card["title"], "New patient: Ahmed")
        self.assertIsNone((await self.fake.get_confirm(self.confirm_id())).patient_id)
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertEqual(len(self.fake.patients), 2)

    async def test_picking_a_candidate_produces_the_ordinary_confirm_card(
            self) -> None:
        self.board(self.ahmed(), self.second_ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient",
                               ("p1", 0.6, "shares the first name"),
                               ("p3", 0.6, "shares the first name"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.choose_existing(self.doctor, "p3", self.confirm_id())
        card = self.last_card()
        self.assertEqual(card["title"],
                         "Existing patient: Ahmed Saleh, 61, type 2 diabetes")
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertEqual(len(self.fake.patients), 2)
        self.assertEqual([l.patient_id for l in self.fake.loops.values()], ["p3"])

    async def test_a_patient_from_another_board_is_refused(self) -> None:
        theirs = Patient(id="x1", doctor_id="other", name="Ahmed Ali",
                         created_at=NOW)
        self.fake.patients["x1"] = theirs
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.choose_existing(self.doctor, "x1", self.confirm_id())
        self.assertEqual(self.sent[-1][1].text, "That patient is gone.")

    # lookup -----------------------------------------------------------------
    async def test_a_lookup_lists_and_creates_nothing_at_all(self) -> None:
        self.board(self.ahmed(), self.second_ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("lookup", ("p1", 0.9, "name matches"),
                               ("p3", 0.9, "name matches"))
        await self.dictate("look for a patient named Ahmed")
        card = self.last_card()
        self.assertTrue(card["title"].startswith("Lookup:"))
        self.assertEqual(self.action_ids(card), ["openpatient:p1", "openpatient:p3"])
        self.assertEqual(self.fake.confirms, {})
        self.assertEqual(self.fake.loops, {})
        self.assertEqual(len(self.fake.patients), 2)
        self.assertIn("Nothing was created", " ".join(card["lines"]))

    async def test_a_lookup_that_finds_nobody_still_writes_nothing(self) -> None:
        self.board(self.ahmed())
        self.record = {**POTASSIUM,
                       "patient": {**POTASSIUM["patient"], "name": "Mariam"}}
        self.verdict = verdict("lookup")
        await self.dictate("look for a patient named Mariam")
        self.assertEqual(self.fake.confirms, {})
        self.assertEqual(self.action_ids(self.last_card()), [])

    # the model is not the decider ------------------------------------------
    async def test_an_explicit_new_patient_overrides_the_model(self) -> None:
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.99, "Ahmed Ali"))
        await self.dictate("this is a new patient, Ahmed, potassium in a week")
        self.assertEqual(self.last_card()["title"], "New patient: Ahmed")

    async def test_a_description_only_match_asks_and_never_attaches(self) -> None:
        salah = Patient(id="p4", doctor_id="d", name="Salah Mahmoud", age=70,
                        diagnosis="atrial fibrillation", created_at=NOW,
                        notes=[{"text": "father of Dr Tarek", "at": "2026-08-01"}])
        self.board(salah)
        self.record = {**POTASSIUM,
                       "patient": {**POTASSIUM["patient"], "name": "Tarek's father"}}
        self.verdict = verdict(
            "existing_patient",
            ("p4", 0.99, "'the father of my friend Tarek' matched the note"))
        await self.dictate("the father of my friend Tarek needs his potassium "
                           "checked in a week")
        card = self.last_card()
        self.assertEqual(card["title"], "Which patient is this?")
        self.assertIn("existing:p4:" + self.confirm_id(), self.action_ids(card))
        self.assertIn("matched the note", " ".join(card["lines"]))

    async def test_a_model_error_falls_back_to_the_matcher_and_the_ask_card(
            self) -> None:
        async def boom(text, rows, extracted_name=""):
            return None

        self.board(self.ahmed())
        self.record = POTASSIUM
        with patch.object(identify, "identify", boom):
            await self.dictate("follow up with Ahmed about his potassium")
        card = self.last_card()
        self.assertEqual(card["title"], "Which patient is this?")
        self.assertIn("existing:p1:" + self.confirm_id(), self.action_ids(card))

    # notes ------------------------------------------------------------------
    # codex re-audit 2. Every note below is words the doctor actually said, and
    # that is not a detail of the test: `identify.clean_note` refuses anything
    # else in code now, so a note the dictation does not carry never reaches a
    # record at all. The drops are proved further down.
    async def test_the_note_is_stored_on_a_new_record_at_confirm_time(
            self) -> None:
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note="daughter of Hend Ismail")
        await self.dictate("new patient, Mariam Fouad, the daughter of Hend "
                           "Ismail, anaemia")
        self.assertEqual(self.fake.patients, {})  # nothing yet
        await registrar.commit(self.doctor, self.confirm_id())
        made = [p for p in self.fake.patients.values()][0]
        self.assertEqual([n["text"] for n in made.notes],
                         ["daughter of Hend Ismail"])
        self.assertEqual(made.notes[0]["at"], "2026-08-29")

    async def test_a_valid_note_is_printed_on_the_card_before_the_tap(
            self) -> None:
        """He reads the note on the card, which is where he can still say no."""
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note="daughter of Hend Ismail")
        await self.dictate("new patient, Mariam Fouad, the daughter of Hend "
                           "Ismail, anaemia")
        self.assertIn("note: daughter of Hend Ismail",
                      self.last_card()["lines"])

    async def test_a_note_longer_than_twelve_words_is_dropped(self) -> None:
        long_note = ("the daughter of Hend Ismail who comes with her mother "
                     "every single time she visits")
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note=long_note)
        await self.dictate("new patient, Mariam Fouad, anaemia, " + long_note)
        await registrar.commit(self.doctor, self.confirm_id())
        made = [p for p in self.fake.patients.values()][0]
        self.assertEqual(made.notes, [])
        self.assertNotIn("note:", " ".join(self.last_card()["lines"]))

    async def test_a_note_carrying_a_dose_is_dropped(self) -> None:
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note="started on 40 mg")
        await self.dictate("new patient, Mariam Fouad, anaemia, started on 40 mg")
        await registrar.commit(self.doctor, self.confirm_id())
        made = [p for p in self.fake.patients.values()][0]
        self.assertEqual(made.notes, [])

    async def test_a_dropped_note_says_so_on_the_board(self) -> None:
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note="started on 40 mg")
        await self.dictate("new patient, Mariam Fouad, anaemia, started on 40 mg")
        dropped = [e for e in self.fake.events.values()
                   if e.text.startswith("identification note dropped")]
        self.assertEqual(len(dropped), 1)
        self.assertIn("number", dropped[0].meta["why"])

    async def test_a_note_the_doctor_never_said_is_dropped(self) -> None:
        self.record = NEW_RECORD
        self.verdict = verdict("new_patient", note="lives in Zagazig")
        await self.dictate("new patient, Mariam Fouad, 33, anaemia")
        await registrar.commit(self.doctor, self.confirm_id())
        made = [p for p in self.fake.patients.values()][0]
        self.assertEqual(made.notes, [])

    async def test_a_note_on_an_existing_record_is_appended_not_replaced(
            self) -> None:
        salah = Patient(id="p4", doctor_id="d", name="Salah Mahmoud", age=70,
                        diagnosis="atrial fibrillation", created_at=NOW,
                        notes=[{"text": "father of Dr Tarek", "at": "2026-08-01"}])
        self.board(salah)
        self.record = {**POTASSIUM,
                       "patient": {**POTASSIUM["patient"], "name": "Salah Mahmoud"}}
        self.verdict = verdict("existing_patient", ("p4", 0.95, "Salah Mahmoud"),
                               note="lives in Zagazig")
        await self.dictate("Salah Mahmoud, who lives in Zagazig, needs his "
                           "potassium checked in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        after = await self.fake.get_patient("p4")
        self.assertEqual([n["text"] for n in after.notes],
                         ["father of Dr Tarek", "lives in Zagazig"])

    async def test_the_notes_reach_the_identification_the_next_time(self) -> None:
        salah = Patient(id="p4", doctor_id="d", name="Salah Mahmoud", age=70,
                        diagnosis="atrial fibrillation", created_at=NOW,
                        notes=[{"text": "father of Dr Tarek", "at": "2026-08-01"}])
        self.board(salah)
        self.record = NEW_RECORD
        await self.dictate("the father of my friend Tarek")
        self.assertEqual(self.board_seen[0].notes,
                         ("father of Dr Tarek (2026-08-01)",))

    async def test_the_patient_page_serves_the_notes(self) -> None:
        salah = Patient(id="p4", doctor_id="d", name="Salah Mahmoud", age=70,
                        diagnosis="atrial fibrillation", created_at=NOW,
                        notes=[{"text": "father of Dr Tarek", "at": "2026-08-01"}])
        self.board(salah)
        page = await sanad_main.patient_view("p4", self.doctor)
        self.assertEqual(page["patient"]["notes"],
                         [{"text": "father of Dr Tarek", "at": "2026-08-01"}])

    # the link, and the self-intro flow -------------------------------------
    async def test_a_new_record_still_mints_the_link_for_the_record_it_made(
            self) -> None:
        self.record, self.verdict = NEW_RECORD, verdict("new_patient")
        await self.dictate("new patient, Mariam Fouad, 33, anaemia")
        await registrar.commit(self.doctor, self.confirm_id())
        made = [p for p in self.fake.patients.values()][0]
        self.assertEqual(self.minted, [made.id])

    async def test_an_addition_mints_no_second_link_for_a_bound_patient(
            self) -> None:
        bound = self.ahmed()
        bound.channels = {"web": True, "telegram_chat_id": 4242}
        self.board(bound)
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertEqual(self.minted, [])
        after = await self.fake.get_patient("p1")
        self.assertEqual(after.channels["telegram_chat_id"], 4242)

    async def test_an_addition_schedules_only_the_new_loop(self) -> None:
        self.board(self.ahmed())
        self.fake.loops["old"] = Loop(
            id="old", patient_id="p1", doctor_id="d", type="VISIT",
            title="Return visit", state="open", created_at=NOW, updated_at=NOW)
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.95, "Ahmed Ali"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        await registrar.commit(self.doctor, self.confirm_id())
        self.assertNotIn("old", self.scheduled)
        self.assertEqual(len(self.scheduled), 1)

    # the action route -------------------------------------------------------
    async def test_the_three_button_ids_round_trip_through_the_action_route(
            self) -> None:
        self.board(self.ahmed(), self.second_ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient",
                               ("p1", 0.6, "shares the first name"),
                               ("p3", 0.6, "shares the first name"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        confirm_id = self.confirm_id()
        request = SimpleNamespace(base_url="https://example/")

        await sanad_main.action(
            sanad_main.ActionIn(action_id=f"existing:p3:{confirm_id}"),
            request, self.doctor)
        self.assertEqual((await self.fake.get_confirm(confirm_id)).patient_id, "p3")

        await sanad_main.action(
            sanad_main.ActionIn(action_id=f"newpatient:{confirm_id}"),
            request, self.doctor)
        self.assertIsNone((await self.fake.get_confirm(confirm_id)).patient_id)

        out = await sanad_main.action(
            sanad_main.ActionIn(action_id="openpatient:p1"), request, self.doctor)
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.fake.patients), 2)

    async def test_the_ask_card_is_retired_by_the_button_that_answers_it(
            self) -> None:
        from core import cards

        self.board(self.ahmed(), self.second_ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient",
                               ("p1", 0.6, "one"), ("p3", 0.6, "two"))
        await self.dictate("follow up with Ahmed about his potassium in a week")
        confirm_id = self.confirm_id()
        await sanad_main.action(
            sanad_main.ActionIn(action_id=f"existing:p1:{confirm_id}"),
            SimpleNamespace(base_url="https://example/"), self.doctor)
        asked = [e for e in self.fake.events.values()
                 if (e.meta.get("card") or {}).get("title") == "Which patient is this?"]
        self.assertEqual(len(asked), 1)
        self.assertFalse(cards.is_open(asked[0]))

    async def test_a_real_patient_id_is_carried_whole(self) -> None:
        """Only the proposal id is shortened. The record's id is never guessed."""
        self.board(self.ahmed())
        self.record = POTASSIUM
        self.verdict = verdict("existing_patient", ("p1", 0.5, "unsure"))
        await self.dictate("follow up with Ahmed about his potassium")
        ids = self.action_ids(self.last_card())
        self.assertIn(f"existing:p1:{self.confirm_id()}", ids)

    def confirm_id(self) -> str:
        return list(self.fake.confirms)[0]


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class EveryButtonFitsInsideATelegramCallback(unittest.TestCase):
    """A callback_data over 64 bytes is a card the doctor never receives.

    Telegram refuses the whole sendMessage, not the one button, so this is not
    a cosmetic limit. "existing:<patient id>:<proposal id>" carries two ids in
    one action, and two full uuid hex strings with the verb on the front are 74
    bytes. The patient id stays whole because the route validates it against the
    doctor's own board; the proposal id is what gets shortened, and this is the
    arithmetic that says that is enough.
    """

    def worst_case(self) -> tuple:
        import uuid

        return uuid.uuid4().hex, uuid.uuid4().hex[:registrar.CONFIRM_ID_LENGTH]

    def test_the_arithmetic_leaves_room(self) -> None:
        longest = len("existing:") + 32 + len(":") + registrar.CONFIRM_ID_LENGTH
        self.assertLessEqual(longest, registrar.TELEGRAM_CALLBACK_LIMIT)

    def test_every_card_the_registrar_builds_stays_inside_it(self) -> None:
        patient_id, confirm_id = self.worst_case()
        record = ProposedRecord.model_validate(POTASSIUM)
        rows = [identify.BoardRow(id=patient_id, name="Ahmed Ali", age=58,
                                  diagnosis="heart failure")]
        held = Patient(id=patient_id, doctor_id="d", name="Ahmed Ali", age=58,
                       diagnosis="heart failure", created_at=NOW)
        asked = identify.Outcome(kind=identify.ASK,
                                 candidates=((patient_id, "the name matches"),))
        listed = identify.Outcome(kind=identify.LIST,
                                  candidates=((patient_id, "the name matches"),))
        built = (registrar.ask_card(record, confirm_id, rows, asked),
                 registrar.lookup_card("Ahmed", rows, listed),
                 registrar.confirm_card(record, confirm_id, "Dr Mohamed",
                                        existing=held),
                 registrar.confirm_card(record, confirm_id, "Dr Mohamed"))
        for card in built:
            for action in card["actions"]:
                with self.subTest(action=action["id"]):
                    self.assertLessEqual(len(action["id"].encode("utf-8")),
                                         registrar.TELEGRAM_CALLBACK_LIMIT)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheActionRouteKnowsTheNewVerbs(unittest.TestCase):
    def test_all_three_are_named_in_the_route(self) -> None:
        from pathlib import Path

        source = Path(sanad_main.__file__).read_text(encoding="utf-8")
        for verb in ('elif verb == "existing"', 'elif verb == "newpatient"',
                     'elif verb == "openpatient"'):
            self.assertIn(verb, source)

    def test_telegram_drives_the_same_three(self) -> None:
        from pathlib import Path

        router = (Path(sanad_main.__file__).parent / "core" / "tg_router.py"
                  ).read_text(encoding="utf-8")
        for verb in ('verb == "existing"', 'verb == "newpatient"',
                     'verb == "openpatient"'):
            self.assertIn(verb, router)


if __name__ == "__main__":
    unittest.main()
