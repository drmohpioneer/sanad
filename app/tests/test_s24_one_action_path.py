"""S24-C: one doctor-action path, whichever door the tap comes through.

Sanad had two corridors into the same records. The console went through the
action route, which claimed the card, took the action key, did the work and
retired the card. Telegram callbacks did a partial version of that in the
router: the action key for five verbs, the domain calls inline, and no card
claim or card resolve at all. So the same button meant two different things
depending on where it was pressed, and the records drifted apart in a way a
doctor could see: a question answered on his phone left the card that asked it
open on his board for ever.

These tests are about that seam and nothing else:

* a duplicate tap does the work once and says the same thing twice;
* a web press and a phone tap on one action produce exactly one effect, in
  either order and at the same instant;
* the phone's two-step answer retires the card that asked the question, so the
  orphan (an answered relay under an open card) cannot be made from this path;
* a compose window belongs to the record, not to the surface that opened it,
  so the answer and /cancel both cross channels, while the ten-minute expiry
  is untouched;
* and the router stays an edge: it may not call a domain mutation itself.

Nothing here reaches a cloud service. `tests/gate0b/memory.py` is the same
transaction-shaped store the nine-beat replay runs on, so the card claim, the
action key and the resolved flag behave as they do in Firestore.
"""

from __future__ import annotations

import ast
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import main as sanad_main
from core import (
    adapters,
    cards,
    concierge,
    coordinator,
    dispatch,
    doctor_actions,
    extractor,
    registrar,
    tg_router,
)
from core.adapters import InboundMessage, OutboundMessage
from core.models import Doctor, Event, Patient, Relay
from tests.gate0b.memory import MemoryStore, patched_store


NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
CHAT = 550123
BASE_URL = "https://sanad.test/"
ROUTER = Path(__file__).resolve().parents[1] / "core" / "tg_router.py"


class RequestStub:
    """Only what the action route reads off a request."""

    base_url = BASE_URL


class RecordingFanout:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: str, message: OutboundMessage) -> str:
        self.sent.append((target, message.text))
        return f"event-{len(self.sent)}"

    def to(self, prefix: str) -> list[str]:
        return [text for target, text in self.sent if target.startswith(prefix)]


class OneActionPath(unittest.IsolatedAsyncioTestCase):
    """The shared fixture: a memory store, a recording fanout, no provider."""

    def setUp(self) -> None:
        self.memory = MemoryStore(start=NOW)
        self.enterContext(patched_store(self.memory))
        self.out = RecordingFanout()
        for module in (adapters, concierge, coordinator, dispatch, extractor,
                       registrar):
            self.enterContext(patch.object(module, "fanout", lambda: self.out))
        self.prompts: list[tuple[Any, str]] = []

        async def send_edge_card(chat_id, text, *args, **kwargs):
            self.prompts.append((chat_id, text))

        self.enterContext(
            patch.object(tg_router, "send_edge_card", send_edge_card))
        self.toasts: list[str] = []

        async def answer_callback(callback_id: str, note: str) -> None:
            self.toasts.append(note)

        self.enterContext(
            patch.object(tg_router.telegram, "answer_callback", answer_callback))

    # -- fixtures ----------------------------------------------------------
    async def a_doctor(self, **fields: Any) -> Doctor:
        doctor = Doctor(id="doctor-1", name="Dr Sanad", web_token="doctor-token",
                        telegram_chat_id=CHAT, created_at=NOW, **fields)
        self.memory.doctors[doctor.id] = doctor
        return doctor

    async def a_patient(self) -> Patient:
        return await self.memory.create_patient(Patient(
            id="patient-1", doctor_id="doctor-1", name="Ahmed Ali",
            diagnosis="hypertension", plan_text="Measure blood pressure.",
            created_at=NOW))

    async def a_card(self, event_id: str, action_id: str, *,
                     label: str = "Confirm") -> Event:
        return await self.memory.add_event(Event(
            id=event_id, doctor_id="doctor-1", kind="system",
            text="a card the doctor has to act on",
            meta={"card": {"severity": "amber",
                           "actions": [{"id": action_id, "label": label}],
                           "decided_by": "code (test fixture)"}},
            ts=NOW))

    async def a_relay(self) -> Relay:
        return await self.memory.save_relay(Relay(
            id="relay-1", doctor_id="doctor-1", patient_id="patient-1",
            question="Can I stop the medicine?", created_at=NOW))

    def tap(self, action_id: str, callback_id: str = "cb-1") -> dict[str, Any]:
        return {"id": callback_id, "data": action_id,
                "message": {"chat": {"id": CHAT}}}

    async def press(self, doctor: Doctor, action_id: str, text: str = "") -> dict:
        return await sanad_main._legacy_action(
            sanad_main.ActionIn(action_id=action_id, text=text),
            RequestStub(), doctor)

    async def resolved(self, event_id: str) -> bool:
        return cards.is_resolved(await self.memory.get_event(event_id))

    async def reload(self, doctor: Doctor) -> Doctor:
        fresh = await self.memory.doctor_by_id(doctor.id)
        assert fresh is not None
        return fresh

    async def says(self, doctor: Doctor, text: str, *, channel: str) -> None:
        await dispatch.handle_inbound(InboundMessage(
            channel=channel, sender_ref=f"doctor:{doctor.web_token}", text=text))


class ATapIsCarriedOutOnceWhereverItComesFrom(OneActionPath):
    async def test_a_duplicate_telegram_tap_does_the_work_once(self) -> None:
        doctor = await self.a_doctor()
        await self.a_card("card-1", "confirm:c1")
        commit = AsyncMock()

        with patch.object(doctor_actions.registrar, "commit", commit):
            await tg_router._callback(self.tap("confirm:c1", "cb-1"), BASE_URL)
            await tg_router._callback(self.tap("confirm:c1", "cb-2"), BASE_URL)

        commit.assert_awaited_once()
        self.assertEqual(doctor.id, commit.await_args.args[0].id)
        # The acknowledgement is the same line both times: a doctor who taps
        # twice must not be told two different things about one fact.
        self.assertEqual(["confirmed", "confirmed"], self.toasts)
        self.assertTrue(await self.resolved("card-1"))

    async def test_a_web_press_then_a_telegram_tap_is_one_effect(self) -> None:
        doctor = await self.a_doctor()
        await self.a_card("card-1", "confirm:c1")
        commit = AsyncMock()

        with patch.object(doctor_actions.registrar, "commit", commit):
            body = await self.press(doctor, "confirm:c1")
            await tg_router._callback(self.tap("confirm:c1"), BASE_URL)

        commit.assert_awaited_once()
        self.assertEqual({"ok": True, "resolved": ["card-1"]}, body)
        self.assertEqual(["confirmed"], self.toasts)

    async def test_a_telegram_tap_then_a_web_press_is_one_effect(self) -> None:
        doctor = await self.a_doctor()
        await self.a_card("card-1", "confirm:c1")
        commit = AsyncMock()

        with patch.object(doctor_actions.registrar, "commit", commit):
            await tg_router._callback(self.tap("confirm:c1"), BASE_URL)
            body = await self.press(doctor, "confirm:c1")

        commit.assert_awaited_once()
        self.assertEqual(
            {"ok": False, "already": True, "action_id": "confirm:c1",
             "detail": "already done"},
            body,
            "the web door lost the legacy refusal body the console reads",
        )
        self.assertTrue(await self.resolved("card-1"))

    async def test_both_doors_at_the_same_instant_is_still_one_effect(self) -> None:
        doctor = await self.a_doctor()
        await self.a_card("card-1", "confirm:c1")
        commit = AsyncMock()

        with patch.object(doctor_actions.registrar, "commit", commit):
            await asyncio.gather(
                self.press(doctor, "confirm:c1"),
                tg_router._callback(self.tap("confirm:c1"), BASE_URL),
            )

        commit.assert_awaited_once()

    async def test_a_button_nothing_carries_out_is_still_an_unknown_button(
        self,
    ) -> None:
        await self.a_doctor()
        await tg_router._callback(self.tap("teleport:c1"), BASE_URL)
        self.assertEqual(["unknown button"], self.toasts)


class TheTwoStepAnswerLeavesNoOrphan(OneActionPath):
    """An answered relay under an open card was the drift a doctor could see."""

    async def _answered_from(self, channel: str) -> Doctor:
        doctor = await self.a_doctor()
        await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")
        await tg_router._callback(self.tap("reply:relay-1"), BASE_URL)
        self.assertEqual(["waiting for your answer"], self.toasts)
        self.assertEqual(1, len(self.prompts))
        doctor = await self.reload(doctor)
        self.assertEqual("relay-1", doctor.awaiting_relay_id)
        await self.says(doctor, "Yes, keep taking it until we review.",
                        channel=channel)
        return await self.reload(doctor)

    async def test_a_relay_answered_from_the_phone_retires_its_card(self) -> None:
        doctor = await self._answered_from("telegram")

        relay = await self.memory.get_relay("relay-1")
        self.assertEqual("answered", relay.state)
        self.assertTrue(
            await self.resolved("card-1"),
            "the relay was answered and its card stayed open: the orphan is back",
        )
        self.assertIsNone(doctor.awaiting_relay_id)
        self.assertEqual(1, len(self.out.to("patient:")))

    async def test_two_answers_at_once_reach_the_patient_once(self) -> None:
        """One question, two answers, the same instant: one send, one addendum.

        Nothing before the action claim can separate them: both are inside the
        ten-minute window and both find the relay open. The claim does, which
        is the point of landing the answer through the same unit as the
        button. The loser is told the question is finished rather than sending
        the patient a second message and growing the plan a second line.
        """
        doctor = await self.a_doctor()
        patient = await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")
        await tg_router._callback(self.tap("reply:relay-1"), BASE_URL)
        doctor = await self.reload(doctor)

        bodies = await asyncio.gather(
            doctor_actions.perform(doctor, "reply:relay-1", "Keep taking it."),
            doctor_actions.perform(doctor, "reply:relay-1", "Actually stop it."),
        )

        self.assertEqual(1, len(self.out.to("patient:")))
        self.assertEqual(
            [True], [body["ok"] for body in bodies if body["ok"]],
            "two answers to one question both carried out")
        self.assertEqual(
            [doctor_actions.already("reply:relay-1")],
            [body for body in bodies if not body["ok"]])
        self.assertEqual("answered", (await self.memory.get_relay("relay-1")).state)
        self.assertTrue(await self.resolved("card-1"))
        plan = (await self.memory.get_patient(patient.id)).plan_text
        self.assertEqual(1, plan.count(concierge.ADDENDUM_HEADER))
        self.assertIsNone((await self.reload(doctor)).awaiting_relay_id)

    async def test_a_note_still_leaves_the_lab_card_open(self) -> None:
        """"Send a note" is a side action and stays one on both surfaces."""
        doctor = await self.a_doctor()
        await self.a_card("card-1", "note:loop-1", label="Send a note")
        note_to_patient = AsyncMock()

        await tg_router._callback(self.tap("note:loop-1"), BASE_URL)
        self.assertEqual(["send your note"], self.toasts)
        doctor = await self.reload(doctor)
        with patch.object(doctor_actions.concierge, "note_to_patient",
                          note_to_patient):
            await self.says(doctor, "Repeat the fasting panel.", channel="web")

        note_to_patient.assert_awaited_once()
        self.assertEqual("loop-1", note_to_patient.await_args.args[1])
        self.assertFalse(await self.resolved("card-1"))
        self.assertIsNone((await self.reload(doctor)).awaiting_note_loop_id)


class TheWindowBelongsToTheRecordAndNotToTheChannel(OneActionPath):
    async def test_a_window_opened_on_the_phone_is_answered_on_the_web(
        self,
    ) -> None:
        doctor = await self.a_doctor()
        await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")

        await tg_router._callback(self.tap("reply:relay-1"), BASE_URL)
        doctor = await self.reload(doctor)
        await self.says(doctor, "Keep it going.", channel="web")

        self.assertEqual("answered", (await self.memory.get_relay("relay-1")).state)
        self.assertTrue(await self.resolved("card-1"))

    async def test_a_window_opened_on_the_web_is_answered_on_the_phone(
        self,
    ) -> None:
        doctor = await self.a_doctor()
        await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")

        await doctor_actions.open_answer_window(
            doctor, "reply:relay-1", channel="web")
        doctor = await self.reload(doctor)
        await self.says(doctor, "Keep it going.", channel="telegram")

        self.assertEqual("answered", (await self.memory.get_relay("relay-1")).state)
        self.assertTrue(await self.resolved("card-1"))

    async def test_cancel_crosses_channels_too(self) -> None:
        doctor = await self.a_doctor()
        await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")

        await tg_router._callback(self.tap("reply:relay-1"), BASE_URL)
        doctor = await self.reload(doctor)
        await self.says(doctor, "/cancel", channel="web")

        doctor = await self.reload(doctor)
        self.assertIsNone(doctor.awaiting_relay_id)
        self.assertIsNone(doctor.awaiting_channel)
        self.assertEqual([dispatch.CANCELLED], self.out.to("doctor:"))
        self.assertEqual([], self.out.to("patient:"))
        self.assertEqual("open", (await self.memory.get_relay("relay-1")).state)
        self.assertFalse(await self.resolved("card-1"))

    async def test_the_ten_minute_window_still_closes(self) -> None:
        doctor = await self.a_doctor()
        await self.a_patient()
        await self.a_relay()
        await self.a_card("card-1", "reply:relay-1", label="Answer")
        handle_doctor = AsyncMock()

        await tg_router._callback(self.tap("reply:relay-1"), BASE_URL)
        doctor = await self.reload(doctor)
        self.memory.advance(timedelta(minutes=11))
        with patch.object(dispatch.registrar, "handle_doctor", handle_doctor):
            await self.says(doctor, "New patient Mariam, 42, check HbA1c.",
                            channel="web")

        self.assertIn(dispatch.EXPIRED, self.out.to("doctor:"))
        self.assertEqual([], self.out.to("patient:"))
        self.assertEqual("open", (await self.memory.get_relay("relay-1")).state)
        handle_doctor.assert_awaited_once()
        self.assertIsNone((await self.reload(doctor)).awaiting_relay_id)


class TheRouterStaysAnEdge(unittest.TestCase):
    """A rail, not a note: the router may not grow a second corridor again.

    core/tg_router.py is a channel boundary. What it is allowed to do is say
    who tapped and which button, hand that to the one action path, and speak
    Telegram back. The moment it calls the Registrar, the Concierge or the
    Care Coordinator itself, there are two implementations of one action again
    and they will drift, which is the whole of what S24-C was for.
    """

    FORBIDDEN = ("registrar", "concierge", "coordinator")
    ALLOWED_ACTION_CALLS = {"perform", "open_answer_window"}

    def setUp(self) -> None:
        self.tree = ast.parse(ROUTER.read_text(encoding="utf-8"),
                              filename=str(ROUTER))

    def _calls_on(self, name: str) -> list[str]:
        found = []
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == name):
                found.append(f"{name}.{node.func.attr} (line {node.lineno})")
        return found

    def _imported(self) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(
                    (alias.asname or alias.name).split(".")[0]
                    for alias in node.names
                )
        return names

    def test_the_router_imports_no_domain_specialist(self) -> None:
        imported = self._imported()
        for name in self.FORBIDDEN:
            with self.subTest(module=name):
                self.assertNotIn(name, imported)

    def test_the_router_calls_no_domain_mutation_itself(self) -> None:
        offenders: list[str] = []
        for name in self.FORBIDDEN:
            offenders.extend(self._calls_on(name))
        self.assertEqual(
            [], offenders,
            "core/tg_router.py drives a domain specialist directly instead of "
            "core/doctor_actions.py:\n" + "\n".join(offenders))

    def test_the_router_reaches_an_action_only_through_the_one_path(self) -> None:
        called = {call.split(".", 1)[1].split(" ", 1)[0]
                  for call in self._calls_on("doctor_actions")}
        self.assertTrue(called, "the router no longer routes anything")
        self.assertTrue(
            called <= self.ALLOWED_ACTION_CALLS,
            f"new entry points into the action path: "
            f"{sorted(called - self.ALLOWED_ACTION_CALLS)}")

    def test_the_action_path_speaks_no_provider(self) -> None:
        """The other half of the seam: the shared unit is not an edge either."""
        actions = ROUTER.parent / "doctor_actions.py"
        tree = ast.parse(actions.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        for provider in ("telegram", "adapters", "fastapi"):
            with self.subTest(provider=provider):
                self.assertNotIn(provider, names)


class TheWebDoorOnlyDelegates(unittest.TestCase):
    """A route is a door onto the path, never a second copy of it."""

    def test_an_unknown_verb_is_a_400_on_the_web_and_a_value_error_inside(
        self,
    ) -> None:
        self.assertTrue(issubclass(doctor_actions.UnknownAction, ValueError))
        route = (Path(sanad_main.__file__).read_text(encoding="utf-8")
                 .split("async def _legacy_action(", 1)[1].split("\n@app.", 1)[0])
        self.assertIn("doctor_actions.perform(", route)
        self.assertIn('raise HTTPException(400, "unknown action")', route)


if __name__ == "__main__":
    unittest.main()
