"""The Care Coordinator: what it may do, and what it cannot do at all.

Half of these read the source the way tests/test_gate_order.py does, because
the guarantee IS the shape of the code: a tool that wrote to Firestore from
inside itself, or a patient message built by string formatting instead of by
core/templates.py, would be a different system with the same tests passing.
Those halves run anywhere.

The other half imports the module, which reaches the cloud SDK, so it skips on a
laptop that has none and runs in the image, exactly as tests/test_chaser.py does.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import policy, templates

APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (APP_ROOT / "core" / "coordinator.py").read_text(encoding="utf-8")
CHASER = (APP_ROOT / "core" / "chaser.py").read_text(encoding="utf-8")
CONCIERGE = (APP_ROOT / "core" / "concierge.py").read_text(encoding="utf-8")
EXTRACTOR = (APP_ROOT / "core" / "extractor.py").read_text(encoding="utf-8")
STORE = (APP_ROOT / "core" / "store.py").read_text(encoding="utf-8")

TOOL_SECTION = SOURCE.split("# The tools.", 1)[1].split("TOOL_FUNCTIONS = (", 1)[0]
EXECUTE = SOURCE.split("async def _execute", 1)[1].split("# Building a turn", 1)[0]


class TheToolSurface(unittest.TestCase):
    def test_the_seven_tools_exist_and_there_is_no_eighth(self) -> None:
        for name in policy.TOOLS:
            with self.subTest(name=name):
                self.assertIn(f"async def {name}(", TOOL_SECTION)
        defined = TOOL_SECTION.count("async def ")
        self.assertEqual(defined, len(policy.TOOLS))

    def test_no_tool_writes_or_sends_anything_itself(self) -> None:
        """The model chooses; code decides, and then code acts. A tool that
        could write would be a model that could write."""
        for forbidden in ("store.update_loop", "fanout()", "append_event",
                          "tasks.enqueue", "update_patient"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, TOOL_SECTION)

    def test_every_tool_goes_through_the_guards(self) -> None:
        self.assertEqual(TOOL_SECTION.count("propose("), len(policy.TOOLS))
        self.assertIn("policy_module.check(", SOURCE)

    def test_only_one_action_per_wake_up(self) -> None:
        propose = SOURCE.split("def propose(", 1)[1].split("_turn:", 1)[0]
        self.assertIn("if self.decision is not None:", propose)
        self.assertIn("ONE_ACTION", propose)


class TheCoordinatorNeverWritesASentence(unittest.TestCase):
    def test_every_patient_message_is_a_template_or_the_doctors_own_line(self) -> None:
        sends = SOURCE.count('f"patient:{turn.patient.id}"')
        self.assertEqual(sends, 2)  # _say, and the doctor's pre-approved reason
        self.assertIn("templates.render(key", SOURCE)
        self.assertIn("_reason_line(turn)", SOURCE)

    def test_the_pre_approved_reason_is_the_doctors_or_a_template(self) -> None:
        reason = SOURCE.split("def _reason_line", 1)[1].split("async def _escalate", 1)[0]
        self.assertIn("policy.followup_reason", reason)
        self.assertIn('templates.render("followup_reason"', reason)

    def test_a_cost_barrier_is_escalated_and_not_discussed(self) -> None:
        self.assertIn("escalate_only()", EXECUTE)
        self.assertIn('_say(turn, "cost_told"', EXECUTE)
        self.assertIn("paused=True", EXECUTE)

    def test_every_escalation_produces_a_doctor_card(self) -> None:
        """escalate_barrier is always allowed, and it always lands on a card."""
        escalate = SOURCE.split("async def _escalate", 1)[1].split(
            "async def _schedule_task", 1)[0]
        self.assertIn("_card(turn,", escalate)
        self.assertIn('"escalation"', escalate)
        self.assertIn('_escalate(turn, decision, barrier)', EXECUTE)

    def test_a_second_refusal_escalates(self) -> None:
        self.assertIn("reluctance", EXECUTE)
        self.assertIn("if reluctance >= 2:", EXECUTE)


class ItFailsClosedToTheLadder(unittest.TestCase):
    def test_the_audit_line_is_the_one_the_spec_names(self) -> None:
        self.assertIn('LADDER_FALLBACK = "fallback: ladder (model unavailable)"',
                      SOURCE)

    def test_every_failure_returns_none_rather_than_guessing(self) -> None:
        choose = SOURCE.split("async def choose(", 1)[1].split(
            "# Carrying the choice out", 1)[0]
        self.assertIn("except Exception:", choose)
        self.assertIn("return None", choose)
        self.assertIn("asyncio.wait_for", SOURCE)

    def test_standing_down_is_written_to_the_log_with_that_line(self) -> None:
        run = SOURCE.split("async def run(", 1)[1].split("# The three doors in", 1)[0]
        self.assertIn("if decision is None:", run)
        self.assertIn("LADDER_FALLBACK", run)


class TheChaserStillOwnsTheLadder(unittest.TestCase):
    """S6 acceptance 2: a wake-up with no reply still produces the ladder nudge."""

    def test_the_coordinator_is_asked_before_anything_is_sent(self) -> None:
        """The agent still decides before a word leaves the process.

        Rev 17 moved the ledger claim in front of the agent turn, so the order
        this asserts is claim, then agent, then send. What must never happen is
        a nudge going out before the Coordinator was asked what this wake-up
        was for, and that is the index compared here.
        """
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertLess(fire.index("coordinator.on_wake"), fire.index("fanout()"))

    def test_the_replay_ledger_sits_in_front_of_the_model_call(self) -> None:
        """A replayed task must cost nothing: no model call, no message.

        Until rev 17 `claim_send` ran after the agent turn, so a Cloud Tasks
        retry paid for a second Coordinator turn and could send a second
        template the ledger never recorded.
        """
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertLess(fire.index("claim_send"), fire.index("coordinator.on_wake"))
        self.assertLess(fire.index('"already sent"'),
                        fire.index("coordinator.on_wake"))

    def test_the_receipt_travels_with_the_wake_up(self) -> None:
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn("receipt=send.id", fire)
        self.assertIn("receipt: str = \"\"", SOURCE)

    def test_a_stand_down_falls_through_to_the_ladder_send(self) -> None:
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn("carried = await coordinator.on_wake", fire)
        self.assertIn("if carried is not None:", fire)
        self.assertLess(fire.index("if carried is not None:"),
                        fire.index("fanout()"))

    def test_the_ladder_passes_the_same_guards_the_agent_does(self) -> None:
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn("policy.check(", fire)
        self.assertIn('"schedule_next_contact"', fire)

    def test_a_paused_loop_is_never_nudged(self) -> None:
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn("if loop.paused:", fire)
        self.assertLess(fire.index("if loop.paused:"), fire.index("claim_send"))

    def test_a_contact_is_counted_even_though_attempts_resets(self) -> None:
        """`attempts` resets on a reply; `contacts` never does.

        The count moved twice. First to a server-side increment (codex item
        13), and then into the reservation (codex re-audit 6), which reads the
        patient's day and the loop's count and spends both in one transaction
        before the model is asked anything. What is asserted here is that the
        ladder still spends a contact of its own, and that it does it
        atomically.
        """
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertIn("store.reserve_contact(", fire)
        self.assertIn("store.LADDER", fire)
        self.assertIn("firestore.ArrayUnion([day_index])", STORE)
        self.assertIn("async_transactional", STORE)

    def test_the_contact_is_reserved_before_the_model_is_asked(self) -> None:
        """codex re-audit 6. The guard that allows it also spends it."""
        fire = CHASER.split("async def fire(", 1)[1]
        self.assertLess(fire.index("store.reserve_contact("),
                        fire.index("coordinator.on_wake("))


class TheConciergeAsksItAfterEveryGate(unittest.TestCase):
    def test_it_sits_after_the_change_gate_and_before_any_generation(self) -> None:
        turn = CONCIERGE.split("async def handle_patient_message", 1)[1].split(
            "async def open_relay", 1)[0]
        order = [
            turn.index("validator.wants_treatment_change"),
            turn.index("validator.model_change_vote"),
            turn.index("if image_bytes"),
            turn.index("coordinator.on_patient_reply"),
            turn.index("answer(patient, doctor, text, history)"),
            turn.index("validator.validate"),
        ]
        self.assertEqual(order, sorted(order))

    def test_a_reading_goes_to_the_chart_and_not_to_an_agent(self) -> None:
        turn = CONCIERGE.split("async def handle_patient_message", 1)[1].split(
            "async def open_relay", 1)[0]
        self.assertIn("not is_reading(text)", turn)

    def test_a_treatment_change_never_reaches_it(self) -> None:
        turn = CONCIERGE.split("async def handle_patient_message", 1)[1].split(
            "async def open_relay", 1)[0]
        self.assertIn("if not change_reason and not is_reading(text):", turn)

    def test_the_review_button_is_still_the_only_way_the_flag_is_set(self) -> None:
        self.assertEqual(CONCIERGE.count("doctor_reviewed=True"), 1)
        reviewed = CONCIERGE.split("async def mark_reviewed", 1)[1]
        self.assertIn("doctor_reviewed=True", reviewed)


class TheSlipIsVerifiedBeforeItSatisfiesAContract(unittest.TestCase):
    def test_the_checks_run_before_the_loop_is_moved_to_review(self) -> None:
        lab = EXTRACTOR.split("async def _handle_lab", 1)[1]
        self.assertLess(lab.index("verify.check("), lab.index('"pending_review"'))

    def test_an_identity_failure_never_attaches(self) -> None:
        lab = EXTRACTOR.split("async def _handle_lab", 1)[1]
        self.assertIn("if verdict.identity_failed:", lab)
        self.assertIn("loop = None", lab)

    def test_a_result_that_does_not_satisfy_wakes_the_coordinator(self) -> None:
        lab = EXTRACTOR.split("async def _handle_lab", 1)[1]
        self.assertIn("coordinator.on_evidence(", lab)
        self.assertIn("not verdict.satisfies", lab)


# The rest imports the module, which reaches the cloud SDK. The image has it and
# a laptop may not, so this half skips there and the source rails above do not.
try:
    # core.concierge is imported here and not only in the tests below, because
    # it is what the barrier-card tests drive and it reaches the ADK package
    # that core.coordinator only imports inside a function. The skip has to
    # cover everything these tests touch.
    from core import concierge, coordinator  # noqa: F401
    from core.models import Doctor, Loop, Patient
    SDK_MISSING = ""
except ImportError as exc:  # pragma: no cover - the image build always has it
    SDK_MISSING = f"cloud SDK not installed: {exc}"

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheTurnItself(unittest.TestCase):
    def turn(self, **facts):
        doctor = Doctor(id="d", name="Dr Mohamed", web_token="t", created_at=NOW)
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali", created_at=NOW)
        loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                    title="Lipid panel", due_at=NOW + timedelta(days=5),
                    created_at=NOW, updated_at=NOW)
        return coordinator.Turn(
            doctor=doctor, patient=patient, loop=loop, trigger=coordinator.REPLY,
            facts=policy.LoopFacts(now=NOW, due_at=loop.due_at, **facts),
            policy=policy.DEFAULT,
        )

    def test_the_registered_tools_are_the_policy_list_in_order(self) -> None:
        self.assertEqual(
            tuple(f.__name__ for f in coordinator.TOOL_FUNCTIONS), policy.TOOLS
        )

    def test_an_accepted_call_is_remembered_and_a_second_one_is_refused(self) -> None:
        turn = self.turn()
        first = turn.propose("schedule_next_contact", {"days_from_now": 2}, "later")
        second = turn.propose("escalate_barrier", {"barrier": "cost"}, "and this")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "refused")
        self.assertEqual(second["reason"], policy.ONE_ACTION)
        self.assertEqual(turn.decision.tool, "schedule_next_contact")

    def test_a_refused_call_comes_back_with_the_reason_and_leaves_no_decision(
            self) -> None:
        turn = self.turn(contacts=6)
        answer = turn.propose("schedule_next_contact", {"days_from_now": 2}, "later")
        self.assertEqual(answer["status"], "refused")
        self.assertIn("policy limit is 6", answer["reason"])
        self.assertIsNone(turn.decision)
        self.assertEqual(len(turn.refusals), 1)

    def test_the_model_can_choose_again_after_a_refusal(self) -> None:
        turn = self.turn(contacts=6)
        turn.propose("schedule_next_contact", {"days_from_now": 2}, "later")
        second = turn.propose("escalate_barrier", {"barrier": "unclear"},
                              "he is out of contacts and still has not gone")
        self.assertEqual(second["status"], "accepted")


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class WhichLoopAReplyIsAbout(unittest.TestCase):
    def loop(self, loop_id, state="waiting_patient", paused=False, minutes=0):
        made = NOW + timedelta(minutes=minutes)
        return Loop(id=loop_id, patient_id="p", doctor_id="d", type="TEST",
                    title=loop_id, state=state, paused=paused,
                    created_at=made, updated_at=made)

    def test_the_oldest_one_still_being_carried(self) -> None:
        board = [self.loop("first"), self.loop("second", minutes=5)]
        self.assertEqual(coordinator.carrying(board).id, "first")

    def test_a_closed_or_paused_loop_is_not_it(self) -> None:
        self.assertIsNone(coordinator.carrying([self.loop("done", "done")]))
        self.assertIsNone(coordinator.carrying([self.loop("p", paused=True)]))
        self.assertIsNone(coordinator.carrying([]))


# --------------------------------------------------------------------------- #
# Round 2. The barrier card is a door, not a notice; a question is not a
# barrier; and a reply lands on the obligation it is about.
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheBarrierCardCanBeAnswered(unittest.IsolatedAsyncioTestCase):
    """Beat 4c: the doctor answers from the card and the loop resumes.

    Everything the Coordinator touches is faked here, so what is being tested is
    the sequence and nothing else: what the card carries, what the answer does to
    the loop, and how many tasks come out of answering twice.
    """

    def setUp(self) -> None:
        from core import events as events_module, lang, settings
        from core import policy as policy_module
        from core import store as store_module
        from core import tasks as tasks_module
        from unittest.mock import patch

        self.now = NOW
        self.sent: list = []
        self.written: list = []
        self.queued: list = []
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                         title="Lipid panel", details={"test_name": "Lipid panel"},
                         state="waiting_patient", due_at=NOW + timedelta(days=5),
                         created_at=NOW, updated_at=NOW)
        self.relays: dict = {}
        outer = self

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))

        async def update_loop(loop_id, **fields):
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def bump_schedule_version(loop_id):
            outer.loop.schedule_version = int(outer.loop.schedule_version or 0) + 1
            return outer.loop.schedule_version

        async def get_loop(loop_id):
            return outer.loop if loop_id == outer.loop.id else None

        async def get_patient(patient_id):
            return outer.patient if patient_id == outer.patient.id else None

        async def save_relay(relay):
            outer.relays[relay.id] = relay
            return relay

        async def get_relay(relay_id):
            return outer.relays.get(relay_id)

        async def close_relay(relay_id):
            relay = outer.relays.get(relay_id)
            if relay is not None:
                relay.state = "answered"

        async def nothing(*a, **kw):
            return None

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

        # The wave B store surface: the patient-wide contact ledger and the
        # counters that are server-side increments in core/store.py.
        ledger: list = []

        async def add_contact(loop_id, day_index):
            outer.loop.contacts = int(outer.loop.contacts or 0) + 1
            if day_index not in (outer.loop.contact_days or []):
                outer.loop.contact_days = [*(outer.loop.contact_days or []),
                                           day_index]

        async def add_reluctance(loop_id):
            outer.loop.reluctance = int(outer.loop.reluctance or 0) + 1
            return outer.loop.reluctance

        async def claim_resume(loop_id, note):
            if not (outer.loop.paused or outer.loop.barrier):
                return False
            outer.loop.paused = False
            outer.loop.barrier = ""
            outer.loop.barrier_note = note
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
            patch.object(store_module, "get_relay", get_relay),
            patch.object(store_module, "close_relay", close_relay),
            patch.object(store_module, "update_patient", nothing),
            patch.object(store_module, "update_doctor", nothing),
            patch.object(store_module, "now", lambda: outer.now),
            patch.object(events_module, "append_event", append_event),
            patch.object(tasks_module, "enqueue", enqueue),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])
        self.policy_module = policy_module
        self.concierge = concierge

    async def raise_the_barrier(self, barrier: str = "cost") -> None:
        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.REPLY,
            "مش هعمل التحليل عشان غالي",
        )
        decision = self.policy_module.check(
            "classify_barrier", {"barrier": barrier, "resume_in_days": 0},
            turn.facts, turn.policy, reason="the patient says it is too expensive",
        )
        self.assertTrue(decision.allowed)
        await coordinator._execute(turn, decision)

    def the_card(self) -> dict:
        cards = [card for _, _, card in self.sent if card]
        self.assertTrue(cards, "the barrier produced no card at all")
        return cards[-1]

    async def test_a_cost_barrier_produces_a_card_the_doctor_can_answer(self) -> None:
        await self.raise_the_barrier()
        card = self.the_card()
        actions = card["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["label"], "Answer")
        self.assertTrue(actions[0]["input"])
        self.assertTrue(actions[0]["id"].startswith("reply:"))
        self.assertTrue(self.loop.paused)
        self.assertEqual(self.loop.barrier, "cost")

    async def test_the_relay_behind_it_names_the_obligation(self) -> None:
        await self.raise_the_barrier()
        relay = list(self.relays.values())[-1]
        self.assertEqual(relay.loop_id, self.loop.id)
        self.assertEqual(relay.reason, "barrier: cost")
        self.assertEqual(relay.question, "مش هعمل التحليل عشان غالي")

    async def test_the_doctors_answer_unpauses_the_loop_and_schedules_one_contact(
            self) -> None:
        await self.raise_the_barrier()
        relay_id = self.the_card()["actions"][0]["id"].split(":", 1)[1]
        self.queued.clear()
        self.sent.clear()

        await self.concierge.doctor_reply(
            self.doctor, relay_id, "Tell him the government lab does it for 60 EGP."
        )
        self.assertFalse(self.loop.paused)
        self.assertEqual(self.loop.barrier, "")
        self.assertIn("doctor answered", self.loop.barrier_note)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0][0], coordinator.NUDGE_PATH)
        told = [text for ref, text, _ in self.sent if ref.startswith("patient:")]
        # rev 17 item 11: this patient writes Arabic, so the doctor's own words
        # arrive with an Arabic label on them and not an English one.
        label = templates.render("doctor_says", "ar", "m", doctor="Dr Mohamed")
        self.assertEqual(label, "Dr Mohamed بيقولك:")
        self.assertTrue(any(t.startswith(label) for t in told))
        self.assertFalse(any("Dr Mohamed says:" in t for t in told))
        self.assertTrue(any("resumed after the doctor answered" in text
                            for _, text, _ in self.written))

    async def test_answering_twice_does_not_schedule_twice(self) -> None:
        await self.raise_the_barrier()
        relay_id = self.the_card()["actions"][0]["id"].split(":", 1)[1]
        self.queued.clear()
        await self.concierge.doctor_reply(self.doctor, relay_id, "First answer.")
        await self.concierge.doctor_reply(self.doctor, relay_id, "Same again.")
        self.assertEqual(len(self.queued), 1)

    async def test_an_ordinary_concierge_relay_resumes_nothing(self) -> None:
        """A relay with no loop on it must leave the barrier path untouched."""
        relay = await self.concierge.open_relay(
            self.patient, self.doctor, "can I take two?", "asks to change treatment"
        )
        self.queued.clear()
        await self.concierge.doctor_reply(self.doctor, relay.id, "No, keep it at one.")
        self.assertEqual(self.queued, [])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AQuestionIsNotABarrier(unittest.IsolatedAsyncioTestCase):
    """The noise the doctor must never see: a card for every question asked.

    The model is told to answer NONE and call nothing for anything that is not
    about whether the patient is doing this obligation. A turn that ends with no
    tool is an ordinary stand-down, and it must not read like a model outage.
    """

    def setUp(self) -> None:
        from core import events as events_module, lang, settings
        from core import store as store_module
        from unittest.mock import patch

        self.sent: list = []
        self.written: list = []
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                         title="Lipid panel", state="waiting_patient",
                         due_at=NOW + timedelta(days=5), created_at=NOW,
                         updated_at=NOW)
        outer = self

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append((kind, text, kw.get("meta", {})))
            return None

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "ar"

        async def contact_days_for_patient(patient_id):
            return ()   # nobody has heard from Sanad yet in this test

        self.patches = [
            patch.object(coordinator, "fanout", lambda: Fanout()),
            patch.object(events_module, "append_event", append_event),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    def audit_lines(self) -> list:
        return [meta.get("audit", {}).get("line", "") for _, _, meta in self.written]

    async def run_with(self, chooser, trigger: str = ""):
        from unittest.mock import patch
        with patch.object(coordinator, "_choose", chooser):
            return await coordinator.run(
                self.loop, self.patient, self.doctor, trigger or coordinator.REPLY,
                "do I take it before food?",
            )

    async def test_a_turn_that_chooses_nothing_produces_no_card(self) -> None:
        async def chose_none(turn):
            return None

        result = await self.run_with(chose_none)
        self.assertIsNone(result)
        self.assertEqual([card for _, _, card in self.sent if card], [])
        self.assertNotIn("escalation", [kind for kind, _, _ in self.written])

    async def test_it_reads_as_handed_to_the_concierge_not_as_an_outage(self) -> None:
        async def chose_none(turn):
            return None

        await self.run_with(chose_none)
        self.assertIn(coordinator.HANDED_TO_CONCIERGE, self.audit_lines())
        self.assertNotIn(coordinator.LADDER_FALLBACK, self.audit_lines())
        self.assertIn(coordinator.STOOD_DOWN_REPLY,
                      [text for _, text, _ in self.written])

    async def test_the_concierge_is_left_to_answer(self) -> None:
        async def chose_none(turn):
            return None

        from unittest.mock import patch
        with patch.object(coordinator, "_choose", chose_none):
            carried = await coordinator.on_patient_reply(
                self.loop, self.patient, self.doctor, "do I take it before food?"
            )
        self.assertIsNone(carried)

    async def test_the_ladder_wording_is_reserved_for_a_model_that_failed(
            self) -> None:
        async def exploded(turn):
            raise RuntimeError("model unavailable")

        await self.run_with(exploded)
        self.assertIn(coordinator.LADDER_FALLBACK, self.audit_lines())
        self.assertEqual([card for _, _, card in self.sent if card], [])

    async def test_a_wake_up_that_chooses_nothing_still_falls_to_the_ladder(
            self) -> None:
        async def chose_none(turn):
            return None

        result = await self.run_with(chose_none, trigger=coordinator.WAKE)
        self.assertIsNone(result)
        self.assertIn(coordinator.HANDED_TO_LADDER, self.audit_lines())


class TheStandDownWordingIsInTheSource(unittest.TestCase):
    def test_the_ladder_line_is_written_only_where_the_model_failed(self) -> None:
        stood = SOURCE.split("async def _stood_down", 1)[1].split(
            "# ------", 1)[0]
        self.assertIn("if turn.model_failed:", stood)
        self.assertLess(stood.index("LADDER_FALLBACK"),
                        stood.index("STOOD_DOWN_REPLY"))

    def test_the_instruction_gives_the_model_a_way_to_do_nothing(self) -> None:
        instruction = SOURCE.split("INSTRUCTION = ", 1)[1].split(
            "def _history_lines", 1)[0]
        self.assertIn("NONE", instruction)
        self.assertIn("call no tool at all", instruction)

    def test_the_instruction_names_the_verifier_refusal(self) -> None:
        """wave A F8a's other half.

        core/policy.py refuses mark_evidence_received on a loop whose verifier
        said the slip does not satisfy the contract. Without a line for that
        case the model is left choosing between refused tools on a slip that
        arrived: it stands down to the ladder, which is fail-closed and still a
        wasted turn and a patient who hears the wrong thing.
        """
        instruction = SOURCE.split("INSTRUCTION = ", 1)[1].split(
            "def _history_lines", 1)[0]
        verifier = instruction.split("verifier could not accept", 1)
        self.assertEqual(len(verifier), 2, "the case is not in the instruction")
        self.assertIn("escalate_barrier", verifier[1][:200])


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class WhichOfThreeObligationsAReplyIsAbout(unittest.TestCase):
    """One patient, three objectives, which is the recorded run's own board."""

    def setUp(self) -> None:
        def made(minutes):
            return NOW + timedelta(minutes=minutes)

        self.bp = Loop(id="bp", patient_id="p", doctor_id="d", type="MONITOR",
                       title="Blood pressure monitoring",
                       details={"metric": "BP", "schedule": "twice a day"},
                       state="waiting_patient", created_at=made(0),
                       updated_at=made(0))
        self.lab = Loop(id="lab", patient_id="p", doctor_id="d", type="TEST",
                        title="Lipid panel", details={"test_name": "Lipid panel"},
                        state="waiting_patient", created_at=made(1),
                        updated_at=made(1))
        self.visit = Loop(id="visit", patient_id="p", doctor_id="d", type="VISIT",
                          title="Follow-up visit", state="open",
                          created_at=made(2), updated_at=made(2))
        self.board = [self.bp, self.lab, self.visit]

    def test_the_lab_beat_lands_on_the_lab(self) -> None:
        for message in ("المعمل مقفول لحد الأحد",
                        "the lab is closed until Sunday",
                        "مش هعمل التحليل عشان غالي",
                        "I will do the blood test tomorrow"):
            with self.subTest(message=message):
                self.assertIs(coordinator.carrying(self.board, message), self.lab)

    def test_a_visit_message_lands_on_the_visit(self) -> None:
        for message in ("ممكن أجي الخميس بدل الأربع؟",
                        "can I come on Thursday instead of the appointment"):
            with self.subTest(message=message):
                self.assertIs(coordinator.carrying(self.board, message), self.visit)

    def test_a_reading_message_lands_on_the_monitoring_loop(self) -> None:
        for message in ("نسيت أقيس الضغط النهاردة",
                        "I forgot to measure my blood pressure this morning"):
            with self.subTest(message=message):
                self.assertIs(coordinator.carrying(self.board, message), self.bp)

    def test_an_analyte_the_title_never_names_still_finds_its_loop(self) -> None:
        self.assertIs(coordinator.carrying(self.board, "ال LDL بتاعي عالي"),
                      self.lab)

    def test_a_message_about_none_of_them_falls_back_to_the_oldest(self) -> None:
        for message in ("شكرا يا دكتور", "hello", ""):
            with self.subTest(message=message):
                self.assertIs(coordinator.carrying(self.board, message), self.bp)

    def test_a_tie_falls_back_to_the_oldest_rather_than_guessing(self) -> None:
        """Two loops the message fits equally well is not a decision Sanad makes."""
        second_lab = Loop(id="lab2", patient_id="p", doctor_id="d", type="TEST",
                          title="Kidney function tests",
                          details={"test_name": "Kidney function tests"},
                          state="open", created_at=NOW + timedelta(minutes=3),
                          updated_at=NOW + timedelta(minutes=3))
        board = [self.lab, second_lab]
        self.assertIs(coordinator.carrying(board, "the lab is closed"), self.lab)

    def test_one_open_obligation_takes_whatever_arrives(self) -> None:
        self.assertIs(coordinator.carrying([self.visit], "the lab is closed"),
                      self.visit)


# --------------------------------------------------------------------------- #
# Block 3, item 0. Four defects proved live on rev sanad-00015-p6x.
# --------------------------------------------------------------------------- #
class TheAgentTurnIsClosedProperly(unittest.TestCase):
    """0d. Every accepted turn logged an OpenTelemetry ERROR traceback.

    Breaking out of `runner.run_async` left ADK's span to be closed during
    generator finalisation, on another task and therefore another context, and
    OpenTelemetry's `detach` raised there. Nothing was lost, but eleven
    tracebacks in eleven turns is a log a judge reads.
    """

    def test_the_stream_is_closed_where_it_is_read(self) -> None:
        choose = SOURCE.split("async def _first_choice", 1)[1].split(
            "async def _choose(", 1)[0]
        self.assertIn("aclosing(stream)", choose)
        self.assertIn("async for _ in events_stream:", choose)

    def test_nothing_walks_away_from_run_async_any_more(self) -> None:
        body = SOURCE.split("async def _choose(", 1)[1].split(
            "async def choose(", 1)[0]
        self.assertIn("stream = runner.run_async(", body)
        self.assertNotIn("async for _ in runner.run_async(", body)
        self.assertIn("_first_choice(stream, turn)", body)


class TheEvidenceTurnSpeaksThePatientsLanguage(unittest.TestCase):
    """0b, as a source rail. The behaviour test is below, under the SDK skip."""

    def test_only_a_real_reply_decides_the_language(self) -> None:
        turn_for = SOURCE.split("async def _turn_for(", 1)[1].split(
            "async def run(", 1)[0]
        self.assertIn("trigger == REPLY and message", turn_for)
        self.assertIn("lang.for_patient(patient, doctor.id)", turn_for)


class TheModelIsToldToNameOneAnalyte(unittest.TestCase):
    """0c. The guard refuses a list; the instruction stops it being written."""

    def test_the_instruction_asks_for_the_first_missing_one(self) -> None:
        instruction = SOURCE.split("INSTRUCTION = ", 1)[1].split(
            "def _history_lines", 1)[0]
        self.assertIn("ONE analyte, the first missing one", instruction)
        self.assertIn("a list is refused", instruction)

    def test_the_tool_says_the_same_thing_where_the_model_reads_it(self) -> None:
        self.assertIn("Never a list", TOOL_SECTION)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheLanguageOfAnInternalNote(unittest.IsolatedAsyncioTestCase):
    """0b, for real: an Arabic speaker was told about his own slip in English."""

    def setUp(self) -> None:
        from core import lang, settings
        from core import store as store_module
        from unittest.mock import patch

        self.doctor = Doctor(id="d", name="Test Doctor", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                         title="Lipid panel", state="waiting_patient",
                         created_at=NOW, updated_at=NOW)

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "ar"  # everything he has ever written is Arabic

        async def contact_days_for_patient(patient_id):
            return ()   # nobody has heard from Sanad yet in this test

        self.patches = [
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(store_module, "contact_days_for_patient",
                         contact_days_for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def test_a_slip_note_is_answered_in_the_patients_own_language(self) -> None:
        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.EVIDENCE,
            "a result arrived: missing: Triglycerides",
        )
        self.assertEqual(turn.speak, "ar")

    async def test_the_sentence_he_gets_is_the_arabic_one(self) -> None:
        from core import templates

        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.EVIDENCE,
            "a result arrived: missing: Triglycerides",
        )
        line = templates.render("missing_part", turn.speak, turn.who,
                                analyte="Triglycerides")
        self.assertEqual(line, templates.MISSING_PART["ar"]["m"].format(
            analyte="Triglycerides"))

    async def test_a_wake_up_with_no_message_reads_the_record_too(self) -> None:
        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.WAKE)
        self.assertEqual(turn.speak, "ar")

    async def test_a_real_reply_is_still_answered_in_the_language_it_used(
            self) -> None:
        turn = await coordinator._turn_for(
            self.loop, self.patient, self.doctor, coordinator.REPLY,
            "the lab is closed until Sunday",
        )
        self.assertEqual(turn.speak, "en")


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class TheStreamIsDrainedNotAbandoned(unittest.IsolatedAsyncioTestCase):
    """0d, for real: the generator's own finalisation runs in this task."""

    def turn(self):
        doctor = Doctor(id="d", name="Test Doctor", web_token="t", created_at=NOW)
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali", created_at=NOW)
        loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                    title="Lipid panel", due_at=NOW + timedelta(days=5),
                    created_at=NOW, updated_at=NOW)
        return coordinator.Turn(
            doctor=doctor, patient=patient, loop=loop, trigger=coordinator.WAKE,
            facts=policy.LoopFacts(now=NOW, wake=True, due_at=loop.due_at),
            policy=policy.DEFAULT,
        )

    async def test_it_stops_at_the_first_choice_and_closes_the_stream(self) -> None:
        turn = self.turn()
        read: list = []
        closed: list = []

        async def stream():
            try:
                read.append("one")
                yield "one"
                turn.propose("schedule_next_contact", {"days_from_now": 2},
                             "the lab opens on Sunday")
                read.append("two")
                yield "two"
                read.append("three")  # one action per wake-up: never reached
                yield "three"
            finally:
                closed.append(True)

        decision = await coordinator._first_choice(stream(), turn)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.tool, "schedule_next_contact")
        self.assertEqual(read, ["one", "two"])
        self.assertEqual(closed, [True])

    async def test_a_turn_that_chooses_nothing_closes_the_stream_too(self) -> None:
        turn = self.turn()
        closed: list = []

        async def stream():
            try:
                yield "one"
                yield "two"
            finally:
                closed.append(True)

        self.assertIsNone(await coordinator._first_choice(stream(), turn))
        self.assertEqual(closed, [True])

    async def test_a_stream_that_raises_still_reaches_the_fallback(self) -> None:
        """Whatever it does, `choose` turns it into the ladder."""
        turn = self.turn()

        async def exploded(_turn):
            raise RuntimeError("the model is down")

        from unittest.mock import patch
        with patch.object(coordinator, "_choose", exploded):
            self.assertIsNone(await coordinator.choose(turn))
        self.assertTrue(turn.model_failed)


# --------------------------------------------------------------------------- #
# rev 17, item 6: an escalation on a patient reply is the whole answer
# --------------------------------------------------------------------------- #
@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class AnEscalationOnAReplyIsTheAnswer(unittest.IsolatedAsyncioTestCase):
    """The patient hears one fixed line, and the Concierge is never reached.

    Live, on rev sanad-00015-p6x, the second "أنا كويس ليه أرجع؟" produced the
    escalation card AND a model-written sentence arguing that the visit
    mattered, because the escalate branches left `answered` False and
    `on_patient_reply` therefore returned None. Everything below is that path
    with the model stubbed.
    """

    def setUp(self) -> None:
        from core import events as events_module, lang, settings
        from core import store as store_module
        from core import tasks as tasks_module
        from unittest.mock import patch

        self.sent: list = []
        self.written: list = []
        self.concierge_calls = 0
        self.relays: dict = {}
        self.doctor = Doctor(id="d", name="Dr Mohamed", web_token="t",
                             created_at=NOW)
        self.patient = Patient(id="p", doctor_id="d", name="Ahmed Ali",
                               sex="male", created_at=NOW)
        self.loop = Loop(id="l", patient_id="p", doctor_id="d", type="VISIT",
                         title="Follow-up visit", state="waiting_patient",
                         reluctance=1, due_at=NOW + timedelta(days=5),
                         created_at=NOW, updated_at=NOW)
        outer = self

        class Fanout:
            async def send(self, ref, msg):
                outer.sent.append((ref, msg.text, msg.card))

        async def update_loop(loop_id, **fields):
            for key, value in fields.items():
                setattr(outer.loop, key, value)

        async def bump_schedule_version(loop_id):
            outer.loop.schedule_version = int(outer.loop.schedule_version or 0) + 1
            return outer.loop.schedule_version

        async def save_relay(relay):
            outer.relays[relay.id] = relay
            return relay

        async def append_event(doctor_id, kind, text="", **kw):
            outer.written.append((kind, text, kw.get("meta", {})))
            return None

        async def enqueue(path, payload, delay):
            return "task/1"

        async def current():
            return "run1", 86400

        async def for_patient(*a, **kw):
            return "ar"

        async def counted_answer(*a, **kw):
            outer.concierge_calls += 1
            raise AssertionError("the Concierge answered after an escalation")

        # The wave B store surface: the patient-wide contact ledger and the
        # counters that are server-side increments in core/store.py.
        ledger: list = []

        async def add_contact(loop_id, day_index):
            outer.loop.contacts = int(outer.loop.contacts or 0) + 1
            if day_index not in (outer.loop.contact_days or []):
                outer.loop.contact_days = [*(outer.loop.contact_days or []),
                                           day_index]

        async def add_reluctance(loop_id):
            outer.loop.reluctance = int(outer.loop.reluctance or 0) + 1
            return outer.loop.reluctance

        async def claim_resume(loop_id, note):
            if not (outer.loop.paused or outer.loop.barrier):
                return False
            outer.loop.paused = False
            outer.loop.barrier = ""
            outer.loop.barrier_note = note
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
            patch.object(coordinator, "fanout", lambda: Fanout()),
            patch.object(concierge, "fanout", lambda: Fanout()),
            patch.object(concierge, "answer", counted_answer),
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
            patch.object(store_module, "save_relay", save_relay),
            patch.object(store_module, "now", lambda: NOW),
            patch.object(events_module, "append_event", append_event),
            patch.object(tasks_module, "enqueue", enqueue),
            patch.object(settings, "current", current),
            patch.object(lang, "for_patient", for_patient),
        ]
        for one in self.patches:
            one.start()
        self.addCleanup(lambda: [one.stop() for one in self.patches])

    async def reply_with(self, tool: str, args: dict, message: str):
        """One patient reply, with the model stubbed to choose `tool`."""
        from unittest.mock import patch

        async def stub(turn):
            turn.propose(tool, args, "he says he feels fine and will not come")
            return turn.decision

        with patch.object(coordinator, "_choose", stub):
            return await coordinator.on_patient_reply(
                self.loop, self.patient, self.doctor, message
            )

    def to_patient(self) -> list:
        return [text for ref, text, card in self.sent
                if ref.startswith("patient:") and not card]

    def cards(self) -> list:
        return [card for _, _, card in self.sent if card]

    async def test_a_second_refusal_cards_the_doctor_and_answers_the_patient(
            self) -> None:
        result = await self.reply_with(
            "classify_barrier", {"barrier": "asymptomatic", "resume_in_days": 0},
            "أنا كويس ليه أرجع؟",
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["answered"])
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.cards()[0]["severity"], "yellow")
        self.assertEqual(self.concierge_calls, 0)

    async def test_the_patient_gets_exactly_the_fixed_template(self) -> None:
        await self.reply_with(
            "classify_barrier", {"barrier": "asymptomatic", "resume_in_days": 0},
            "أنا كويس ليه أرجع؟",
        )
        wanted = templates.render("told_doctor_will_answer", "ar", "m",
                                  doctor="Dr Mohamed")
        self.assertEqual(self.to_patient(), [wanted])

    async def test_escalate_barrier_answers_the_patient_the_same_way(self) -> None:
        result = await self.reply_with(
            "escalate_barrier", {"barrier": "in_hospital"},
            "أنا في المستشفى دلوقتي",
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["answered"])
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(self.concierge_calls, 0)

    async def test_a_cost_barrier_still_says_the_cost_line_and_nothing_more(
            self) -> None:
        """Cost was already answered before rev 17 and must not double up."""
        result = await self.reply_with(
            "classify_barrier", {"barrier": "cost", "resume_in_days": 0},
            "غالي أوي",
        )
        self.assertTrue(result["answered"])
        self.assertEqual(len(self.to_patient()), 1)
        self.assertEqual(self.to_patient(),
                         [templates.render("cost_told", "ar", "m",
                                           doctor="Dr Mohamed")])

    async def test_a_wake_up_escalation_says_nothing_to_the_patient(self) -> None:
        """No message was waiting, so no contact is spent answering one."""
        from unittest.mock import patch

        async def stub(turn):
            turn.propose("escalate_barrier", {"barrier": "in_hospital"},
                         "nobody has heard from him")
            return turn.decision

        with patch.object(coordinator, "_choose", stub):
            result = await coordinator.on_wake(self.loop, self.patient,
                                               self.doctor, receipt="l:nudge:1")
        self.assertIsNotNone(result)
        self.assertFalse(result["answered"])
        self.assertEqual(self.to_patient(), [])
        self.assertEqual(len(self.cards()), 1)

    async def test_the_unclear_branch_is_still_left_to_the_concierge(self) -> None:
        """The one deliberate exception, and the source says why."""
        result = await self.reply_with(
            "classify_barrier", {"barrier": "unclear", "resume_in_days": 0},
            "مش عارف",
        )
        self.assertIsNone(result)
        self.assertEqual(self.to_patient(), [])
        self.assertEqual(len(self.cards()), 1)

    def test_the_concierge_returns_the_moment_the_coordinator_answered(self) -> None:
        turn = CONCIERGE.split("async def handle_patient_message", 1)[1]
        self.assertIn("if carried is not None:\n                return", turn)


# --------------------------------------------------------------------------- #
# rev 17, item 4: the guard at ADK's own enforcement point
# --------------------------------------------------------------------------- #
class TheGuardIsRegisteredWithTheFramework(unittest.TestCase):
    """It runs where ADK runs it, and the in-tool guard stays as the second line.

    The first two read the source, so they run anywhere. The last two read
    `coordinator.GUARD_ARGS` out of the imported module, so they carry the same
    SDK_MISSING guard the imported half of this file carries: without it the
    name is simply not there on a laptop with no cloud SDK, and the test errors
    instead of skipping.
    """

    def test_the_agent_is_built_with_the_callback(self) -> None:
        choose = SOURCE.split("async def _choose(", 1)[1]
        self.assertIn("before_tool_callback=before_tool", choose)

    def test_the_tool_bodies_still_carry_their_own_guard(self) -> None:
        self.assertEqual(TOOL_SECTION.count("propose("), len(policy.TOOLS))

    @unittest.skipIf(SDK_MISSING, SDK_MISSING)
    def test_the_hook_knows_the_arguments_of_every_tool_and_no_others(self) -> None:
        self.assertEqual(tuple(coordinator.GUARD_ARGS), policy.TOOLS)

    @unittest.skipIf(SDK_MISSING, SDK_MISSING)
    def test_each_named_argument_is_one_the_tool_really_passes(self) -> None:
        """The table and the tool bodies build the same dict, or this fails."""
        for name, wanted in coordinator.GUARD_ARGS.items():
            body = TOOL_SECTION.split(f"async def {name}(", 1)[1].split(
                "\nasync def ", 1)[0]
            for key in wanted:
                with self.subTest(tool=name, key=key):
                    self.assertIn(f'"{key}"', body)


@unittest.skipIf(SDK_MISSING, SDK_MISSING)
class ARefusedCallNeverEntersTheToolBody(unittest.IsolatedAsyncioTestCase):
    """The point of using the framework hook rather than only the tool body.

    ADK calls every `before_tool_callback` before the function, and a dict
    coming back from one is returned to the model AS the tool's answer: the
    function is not entered. That contract is pinned against the installed
    version below, so an SDK upgrade that changed it would fail here rather
    than quietly turn one of the two enforcement points off.
    """

    class FakeTool:
        def __init__(self, name):
            self.name = name

    def setUp(self) -> None:
        self.entered: list = []
        doctor = Doctor(id="d", name="Dr Mohamed", web_token="t", created_at=NOW)
        patient = Patient(id="p", doctor_id="d", name="Ahmed Ali", created_at=NOW)
        loop = Loop(id="l", patient_id="p", doctor_id="d", type="TEST",
                    title="Lipid panel", due_at=NOW + timedelta(days=5),
                    created_at=NOW, updated_at=NOW)
        self.turn = coordinator.Turn(
            doctor=doctor, patient=patient, loop=loop, trigger=coordinator.REPLY,
            facts=policy.LoopFacts(now=NOW, due_at=loop.due_at, contacts=6),
            policy=policy.DEFAULT,
        )
        token = coordinator._turn.set(self.turn)
        self.addCleanup(coordinator._turn.reset, token)

    async def as_adk_would(self, name: str, args: dict):
        """ADK's own three steps, in the order its source runs them."""
        answer = coordinator.before_tool(tool=self.FakeTool(name), args=args,
                                         tool_context=None)
        if answer is not None:
            return answer
        self.entered.append(name)
        function = dict(zip(policy.TOOLS, coordinator.TOOL_FUNCTIONS))[name]
        return await function(**args)

    async def test_a_call_past_the_contact_ceiling_is_refused_at_the_hook(
            self) -> None:
        answer = await self.as_adk_would(
            "schedule_next_contact",
            {"days_from_now": 2, "reason": "he asked for Tuesday"})
        self.assertEqual(answer["status"], "refused")
        self.assertIn("policy limit is 6", answer["reason"])
        self.assertEqual(self.entered, [], "the tool body was entered anyway")
        self.assertIsNone(self.turn.decision)
        self.assertEqual(len(self.turn.refusals), 1)

    async def test_an_allowed_call_reaches_the_body_and_is_ruled_on_once(
            self) -> None:
        self.turn.facts = replace(self.turn.facts, contacts=0)
        answer = await self.as_adk_would(
            "schedule_next_contact",
            {"days_from_now": 2, "reason": "the lab reopens on Sunday"})
        self.assertEqual(answer["status"], "accepted")
        self.assertEqual(self.entered, ["schedule_next_contact"])
        self.assertEqual(self.turn.decision.tool, "schedule_next_contact")
        self.assertEqual(self.turn.refusals, [])

    async def test_a_second_different_tool_is_still_one_action_per_wake_up(
            self) -> None:
        self.turn.facts = replace(self.turn.facts, contacts=0)
        await self.as_adk_would("schedule_next_contact",
                                {"days_from_now": 2, "reason": "later"})
        answer = await self.as_adk_would(
            "escalate_barrier", {"barrier": "cost", "reason": "and this too"})
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["reason"], policy.ONE_ACTION)

    async def test_a_tool_with_no_turn_in_context_is_refused_outright(
            self) -> None:
        coordinator._turn.set(None)
        answer = coordinator.before_tool(
            tool=self.FakeTool("pause_loop"), args={"reason": "x"},
            tool_context=None)
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["reason"], coordinator.NO_TURN)

    async def test_a_name_that_is_not_a_tool_is_refused(self) -> None:
        answer = coordinator.before_tool(
            tool=self.FakeTool("change_the_dose"), args={"mg": 80},
            tool_context=None)
        self.assertEqual(answer["status"], "refused")
        self.assertEqual(answer["reason"], policy.UNKNOWN_TOOL)

    async def test_a_broken_hook_falls_through_to_the_in_tool_guard(self) -> None:
        """The callback never takes the system down; it only ever adds a no."""
        from unittest.mock import patch

        def explode(*a, **kw):
            raise RuntimeError("the hook is broken")

        with patch.object(coordinator.Turn, "precheck", explode):
            answer = coordinator.before_tool(
                tool=self.FakeTool("pause_loop"), args={"reason": "x"},
                tool_context=None)
        self.assertIsNone(answer, "a broken hook must let the body decide")

    def test_the_adk_contract_this_relies_on(self) -> None:
        """Pinned against the installed google-adk, not against memory."""
        import inspect

        from google.adk.flows.llm_flows import functions

        source = inspect.getsource(functions)
        self.assertIn("for before_callback in agent.canonical_before_tool_callbacks",
                      source)
        self.assertIn("if function_response:", source)
        self.assertIn("function_response = await __call_tool_async(", source)
        callbacks = source.index("canonical_before_tool_callbacks")
        call = source.index("function_response = await __call_tool_async(")
        self.assertLess(callbacks, call)


if __name__ == "__main__":
    unittest.main()
