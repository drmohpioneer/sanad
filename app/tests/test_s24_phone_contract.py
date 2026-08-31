"""S24-G: what an enrolled doctor's phone is allowed to say, and what it may not.

The contract is one sentence long and the rest of this file is it, said in
tests: a danger and a time-bounded promise ring the phone now; a verified
result waiting for review and a deadline that ran out are parked and listed in
the 09:00 Cairo digest; anything a sender explicitly marked as quiet work stays
in the cockpit; and anything nobody has classified rings the phone anyway,
because the failure this system may never have is a silent one.

Two of the cases below are the ones that would actually hurt someone, and they
are written first in the file for that reason:

  ``DangerIsUntouched``   the DANGER path still reaches the provider, still
                          raises on a provider ``ok:false``, and that raise
                          still turns core/escalate.told_or_fail_closed False,
                          so the patient is never told his doctor knows when
                          the doctor does not. Parking is not on that path and
                          may never get onto it.
  ``ParkedIsNeverTold``   a parked message reports itself as parked. It never
                          marks the phone channel done, never claims delivery,
                          and its route answers False to the one question
                          escalation-shaped logic asks.

Everything is a local double: no Firestore, no provider, no model.
"""

from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, patch

from core import adapters, digest, escalate, summary, timing
from core.adapters import OutboundMessage, ResolvedTarget
from core.channel_contracts import NotificationClass


DOCTOR_REF = "doctor:web-token"
PATIENT_REF = "patient:p1"
NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)


def enrolled(flag: bool = True) -> ResolvedTarget:
    return ResolvedTarget(
        doctor_id="d1", patient_id=None, synthetic=False, enrolled=flag
    )


def patient_target() -> ResolvedTarget:
    return ResolvedTarget(
        doctor_id="d1", patient_id="p1", synthetic=False, enrolled=False
    )


def review_card() -> dict:
    """The card core/extractor.py builds for a result awaiting review."""
    return {
        "title": "🧪 Lab results · Ahmed",
        "severity": "yellow",
        "lines": ["Potassium 4.1"],
        "actions": [
            {"id": "reviewed:loop-1", "label": "Reviewed"},
            {"id": "note:loop-1", "label": "Send a note", "input": True},
        ],
    }


class Harness(unittest.IsolatedAsyncioTestCase):
    """One Fanout, one recorded provider call, one recorded console event."""

    def setUp(self) -> None:
        adapters._WARNED.clear()
        self.appended: list[dict] = []
        self.marked: list[str] = []
        self.send_card = AsyncMock(
            return_value={"ok": True, "result": {"message_id": 1}}
        )

    async def _append(self, doctor_id, kind, text="", **fields):
        row = {"doctor_id": doctor_id, "kind": kind, "text": text, **fields}
        self.appended.append(row)
        return SimpleNamespace(id=f"event-{len(self.appended)}", **row)

    async def _mark(self, send_id: str, channel: str) -> None:
        self.marked.append(channel)

    async def fan(
        self,
        msg: OutboundMessage,
        *,
        target: ResolvedTarget,
        ref: str = DOCTOR_REF,
        ok: bool = True,
    ) -> tuple[adapters.Fanout, object]:
        out = adapters.Fanout()
        if not ok:
            self.send_card.return_value = {"ok": False, "description": "no chat"}
        with (
            patch.dict(os.environ, {"LEGACY_RUNTIME": "true", "OUTBOX_MODE": "off"}),
            patch.object(
                adapters, "resolve_target", AsyncMock(return_value=target)
            ),
            patch.object(adapters.events, "append_event", self._append),
            patch.object(
                adapters.store, "channels_done", AsyncMock(return_value=frozenset())
            ),
            patch.object(adapters.store, "mark_channel_done", self._mark),
            patch.object(adapters.store, "now", lambda: NOW),
            patch.object(
                adapters.telegram, "chat_id_for", AsyncMock(return_value=7700)
            ),
            patch.object(adapters.telegram, "enabled", return_value=True),
            patch.object(adapters.telegram, "send_card", self.send_card),
        ):
            receipt = await out.send(ref, msg)
        return out, receipt

    def phone_note(self) -> dict:
        """The mark left on the console event, if any."""
        meta = self.appended[-1].get("meta") or {}
        return meta.get(summary.PHONE_META) or {}


# --------------------------------------------------------------------------- #
# The two that would hurt someone
# --------------------------------------------------------------------------- #
class DangerIsUntouched(Harness):
    async def test_danger_still_reaches_the_provider_for_an_enrolled_doctor(
        self,
    ) -> None:
        out, receipt = await self.fan(
            OutboundMessage(
                text="Critical potassium for Ahmed.",
                meta={"notification_class": NotificationClass.DANGER.value},
            ),
            target=enrolled(),
        )
        self.send_card.assert_awaited_once()
        self.assertEqual([adapters.PUSHED], [r.decision for r in out.phone_routes])
        self.assertTrue(out.phone_routes[0].rang_the_phone)
        self.assertEqual("event-1", receipt)
        self.assertEqual({}, self.phone_note(),
                         "a pushed danger card must be stored exactly as before")

    async def test_urgent_sla_still_reaches_the_provider(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Ahmed has not answered the critical callback.",
                meta={"notification_class": NotificationClass.URGENT_SLA.value},
            ),
            target=enrolled(),
        )
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)

    async def test_danger_never_costs_a_recipient_lookup(self) -> None:
        """The push path must not gain a dependency on a store that can be down.

        `route_for` answers every pushing branch before it resolves anybody, so
        a DANGER decision cannot fail because Firestore is unavailable. This
        drives it with a resolver that raises: the answer is still a push.
        """
        with patch.object(
            adapters,
            "resolve_target",
            AsyncMock(side_effect=RuntimeError("firestore is down")),
        ):
            route = await adapters.route_for(
                DOCTOR_REF,
                OutboundMessage(
                    text="Critical potassium.",
                    meta={"notification_class": "DANGER"},
                ),
            )
        self.assertEqual(adapters.PUSHED, route.decision)
        self.assertTrue(route.push)

    async def test_provider_ok_false_still_raises_and_fails_closed(self) -> None:
        """The fail-closed promise, end to end.

        core/escalate.told_or_fail_closed decides "the doctor was told" from
        whether `persist` raised. TelegramAdapter raises on a provider
        ``ok:false``; the phone contract must not swallow it, reorder it, or
        answer the question with anything else.
        """
        written: list[str] = []

        async def persist() -> None:
            await self.fan(
                OutboundMessage(
                    text="Critical potassium for Ahmed.",
                    meta={"notification_class": "DANGER"},
                ),
                target=enrolled(),
                ok=False,
            )

        async def error_event(doctor_id, kind, text="", **fields):
            written.append(text)

        with patch.object(escalate.events, "append_event", error_event):
            landed = await escalate.told_or_fail_closed(
                persist, doctor_id="d1", patient_id="p1", what="escalation"
            )

        self.assertFalse(
            landed, "a rejected Telegram send must still fail closed"
        )
        self.assertTrue(written, "the fail-closed error event was not written")
        self.assertNotIn(
            "telegram", self.marked,
            "a rejected send must never be recorded as a delivered channel",
        )


class ParkedIsNeverTold(Harness):
    async def test_a_parked_message_reports_parked_and_never_rings(self) -> None:
        out, receipt = await self.fan(
            OutboundMessage(
                text="Lab results for Ahmed.",
                patient_id="p1",
                card=review_card(),
                meta={"notification_class": NotificationClass.REVIEW_READY.value},
            ),
            target=enrolled(),
        )
        route = out.phone_routes[0]
        self.assertEqual(adapters.PARKED, route.decision)
        self.assertTrue(route.parked)
        self.assertFalse(
            route.rang_the_phone,
            "a parked message may never read as the doctor having been told",
        )
        self.send_card.assert_not_awaited()
        self.assertEqual(
            [], self.marked,
            "the skipped phone channel must not be recorded as delivered",
        )
        self.assertEqual("event-1", receipt,
                         "parking must not change what send() returns")

    async def test_the_console_card_is_still_written_and_carries_the_mark(
        self,
    ) -> None:
        await self.fan(
            OutboundMessage(
                text="Lab results for Ahmed.",
                patient_id="p1",
                card=review_card(),
                meta={"notification_class": "REVIEW_READY"},
            ),
            target=enrolled(),
        )
        self.assertEqual(1, len(self.appended))
        stored = self.appended[0]
        self.assertEqual("card", stored["kind"])
        self.assertEqual(review_card(), stored["meta"]["card"])
        note = self.phone_note()
        self.assertEqual(summary.PARKED, note["decision"])
        self.assertEqual("REVIEW_READY", note["class"])
        self.assertEqual(
            timing.next_digest_at(NOW).isoformat(), note["release_at"],
            "a parked card must say when the morning gives it back",
        )


# --------------------------------------------------------------------------- #
# The routing table, class by class
# --------------------------------------------------------------------------- #
class TheRoutingTable(Harness):
    async def test_deadline_outcome_is_parked(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Ahmed is not answering.",
                meta={"notification_class": "DEADLINE_OUTCOME"},
            ),
            target=enrolled(),
        )
        self.assertEqual(adapters.PARKED, out.phone_routes[0].decision)
        self.send_card.assert_not_awaited()

    async def test_silent_work_is_suppressed_and_the_morning_owes_nothing(
        self,
    ) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Kept on Ahmed's record.",
                meta={"notification_class": "SILENT_WORK"},
            ),
            target=enrolled(),
        )
        self.assertEqual(adapters.SUPPRESSED, out.phone_routes[0].decision)
        self.send_card.assert_not_awaited()
        self.assertEqual(summary.SUPPRESSED, self.phone_note()["decision"])
        self.assertEqual(
            [], summary.parked_rows([_event(self.appended[0])]),
            "a suppressed message is not owed by the digest",
        )

    async def test_solicited_response_is_suppressed(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Digest for Dr Mohamed.",
                meta={"notification_class": "SOLICITED_RESPONSE"},
            ),
            target=enrolled(),
        )
        self.assertEqual(adapters.SUPPRESSED, out.phone_routes[0].decision)
        self.send_card.assert_not_awaited()

    async def test_unclassified_pushes_and_names_the_callsite(self) -> None:
        message = OutboundMessage(text="Opened Lipid panel for Ahmed, waiting.")
        with self.assertLogs("sanad.adapters", level="WARNING") as caught:
            out, _ = await self.fan(message, target=enrolled())
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)
        self.assertEqual("", out.phone_routes[0].notification_class)
        self.assertIn("Opened Lipid panel", "\n".join(caught.output))
        self.assertEqual(
            {}, self.phone_note(),
            "a pushed message leaves the stored card exactly as it was",
        )

    async def test_a_stamped_legacy_unclassified_is_still_unclassified(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Something nobody has decided about.",
                meta={"notification_class": "LEGACY_UNCLASSIFIED"},
            ),
            target=enrolled(),
        )
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)

    async def test_an_unreadable_class_is_unclassified_and_pushes(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Mistyped class.",
                meta={"notification_class": "REVIEW-READY-ISH"},
            ),
            target=enrolled(),
        )
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)

    async def test_the_warning_names_a_callsite_once_and_not_every_send(
        self,
    ) -> None:
        message = OutboundMessage(text="An ordinary confirmation.")
        with self.assertLogs("sanad.adapters", level="WARNING") as caught:
            await self.fan(message, target=enrolled())
            await self.fan(message, target=enrolled())
        self.assertEqual(1, len(caught.output))


class WhatIsNotCoveredAtAll(Harness):
    async def test_a_patient_bound_message_is_never_routed(self) -> None:
        out, _ = await self.fan(
            OutboundMessage(text="Did you take the medicine?"),
            target=patient_target(),
            ref=PATIENT_REF,
        )
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.LEGACY, out.phone_routes[0].decision)
        self.assertEqual({}, self.phone_note())

    async def test_a_doctor_who_is_not_enrolled_keeps_the_legacy_fanout(
        self,
    ) -> None:
        out, _ = await self.fan(
            OutboundMessage(
                text="Lab results for Ahmed.",
                card=review_card(),
                meta={"notification_class": "REVIEW_READY"},
            ),
            target=enrolled(False),
        )
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.LEGACY, out.phone_routes[0].decision)
        self.assertEqual(
            {}, self.phone_note(),
            "a doctor outside the rollout must store the card byte for byte",
        )

    async def test_a_failed_recipient_lookup_falls_back_to_the_phone(self) -> None:
        with patch.object(
            adapters,
            "resolve_target",
            AsyncMock(side_effect=RuntimeError("firestore is down")),
        ):
            route = await adapters.route_for(
                DOCTOR_REF,
                OutboundMessage(
                    text="Lab results.", meta={"notification_class": "REVIEW_READY"}
                ),
            )
        self.assertEqual(adapters.LEGACY, route.decision)
        self.assertTrue(route.push, "a lookup failure must never go quiet")


# --------------------------------------------------------------------------- #
# Deriving a class the sender did not stamp
# --------------------------------------------------------------------------- #
class DerivedFromTheMessageItself(unittest.TestCase):
    def test_the_reviewed_button_means_a_result_is_waiting_for_review(self) -> None:
        known, why = adapters.classify(
            OutboundMessage(text="Lab results for Ahmed.", card=review_card())
        )
        self.assertIs(NotificationClass.REVIEW_READY, known)
        self.assertIn("Reviewed", why)

    def test_the_exhausted_ladder_means_a_deadline_outcome(self) -> None:
        known, _ = adapters.classify(
            OutboundMessage(
                text="Ahmed is not answering.",
                meta={"decided_by": "code (core/chaser.py, the ladder is exhausted)"},
            )
        )
        self.assertIs(NotificationClass.DEADLINE_OUTCOME, known)

    def test_a_stamped_class_always_beats_a_derived_one(self) -> None:
        known, why = adapters.classify(
            OutboundMessage(
                text="Critical potassium.",
                card=review_card(),
                meta={"notification_class": "DANGER"},
            )
        )
        self.assertIs(NotificationClass.DANGER, known)
        self.assertIn("stamped", why)

    def test_an_ordinary_card_is_not_guessed_at(self) -> None:
        known, _ = adapters.classify(
            OutboundMessage(
                text="Reminders paused for Ahmed.",
                card={"title": "Reminders paused", "severity": "white",
                      "lines": [], "actions": []},
            )
        )
        self.assertIsNone(known, "an unmarked card must stay unclassified")


class EnrollmentIsReadFromTheDoctor(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_carries_the_rollout_flag(self) -> None:
        doctor = SimpleNamespace(
            id="d1", synthetic=False, workspace_facts_enabled=True
        )
        with patch.object(
            adapters.store, "doctor_by_token", AsyncMock(return_value=doctor)
        ):
            target = await adapters.resolve_target(DOCTOR_REF)
        self.assertTrue(target.enrolled)

    async def test_a_doctor_without_the_flag_is_not_enrolled(self) -> None:
        doctor = SimpleNamespace(id="d1", synthetic=False)
        with patch.object(
            adapters.store, "doctor_by_token", AsyncMock(return_value=doctor)
        ):
            target = await adapters.resolve_target(DOCTOR_REF)
        self.assertFalse(target.enrolled)


# --------------------------------------------------------------------------- #
# The morning half
# --------------------------------------------------------------------------- #
def _event(row: dict, ident: str = "event-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=ident,
        doctor_id=row.get("doctor_id", "d1"),
        patient_id=row.get("patient_id"),
        text=row.get("text", ""),
        meta=row.get("meta") or {},
    )


def parked_event(
    ident: str, title: str, patient_id: str = "p1", digest_at: str = ""
) -> SimpleNamespace:
    note = {
        "decision": summary.PARKED,
        "class": "REVIEW_READY",
        "release_at": timing.next_digest_at(NOW).isoformat(),
    }
    if digest_at:
        note["digest_at"] = digest_at
    return SimpleNamespace(
        id=ident,
        doctor_id="d1",
        patient_id=patient_id,
        text=title,
        ts=NOW,
        kind="card",
        meta={"card": {"title": title, "severity": "yellow", "lines": [],
                       "actions": []},
              summary.PHONE_META: note},
    )


class NineOClockCairo(unittest.TestCase):
    def test_the_release_moment_is_the_next_nine_in_cairo(self) -> None:
        released = timing.next_digest_at(NOW)
        local = released.astimezone(timing.CAIRO)
        self.assertEqual(timing.DIGEST_HOUR, local.hour)
        self.assertEqual(0, local.minute)
        self.assertGreater(released, NOW)

    def test_before_nine_the_release_is_this_morning(self) -> None:
        early = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
        released = timing.next_digest_at(early)
        self.assertEqual(
            released.astimezone(timing.CAIRO).date(),
            early.astimezone(timing.CAIRO).date(),
        )

    def test_the_digest_hour_is_the_hour_quiet_time_ends(self) -> None:
        self.assertEqual(timing.QUIET_UNTIL_HOUR, timing.DIGEST_HOUR)


class TheDigestListsWhatThePhoneDidNotSay(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.doctor = SimpleNamespace(
            id="d1", name="Dr Mohamed", lang="en", web_token="web-token"
        )
        self.patients = [
            SimpleNamespace(id="p1", name="Ahmed Ali", diagnosis="hypertension")
        ]
        self.history = [
            parked_event("event-1", "🧪 Lab results · Ahmed Ali"),
            parked_event("event-2", "⚪ Ahmed Ali unreachable"),
            parked_event("event-3", "already read", digest_at=NOW.isoformat()),
        ]
        self.updates: list[tuple[str, dict]] = []

    async def _update_event(self, event_id: str, **fields) -> None:
        self.updates.append((event_id, fields))
        for row in self.history:
            if row.id == event_id:
                row.meta = fields["meta"]

    def _board(self) -> ExitStack:
        stack = ExitStack()
        for patcher in (
            patch.object(
                digest.store, "list_patients",
                AsyncMock(return_value=self.patients),
            ),
            patch.object(digest.store, "list_loops", AsyncMock(return_value=[])),
            patch.object(digest.store, "open_relays", AsyncMock(return_value=[])),
            patch.object(digest.store, "now", lambda: NOW),
            patch.object(
                digest.events, "last_events",
                AsyncMock(side_effect=lambda *a, **k: list(self.history)),
            ),
            patch.object(adapters.store, "now", lambda: NOW),
            patch.object(adapters.store, "update_event", self._update_event),
        ):
            stack.enter_context(patcher)
        return stack

    async def test_parked_items_are_listed_with_patient_and_title(self) -> None:
        with self._board():
            text = await digest.build(self.doctor)

        self.assertIn(summary.PARKED_HEADING, text)
        block = text.split(summary.PARKED_HEADING, 1)[1].split("\n\n", 1)[0]
        self.assertIn("Ahmed Ali: 🧪 Lab results · Ahmed Ali", block)
        self.assertIn("Ahmed Ali: ⚪ Ahmed Ali unreachable", block)
        self.assertNotIn(
            "already read", block,
            "an item a previous digest already released must not repeat",
        )
        self.assertIn(f"{summary.PARKED_HEADING} (2)", text)

    async def test_the_digest_releases_what_it_listed_and_only_once(self) -> None:
        for _ in range(2):
            with self._board():
                await digest.build(self.doctor)

        self.assertEqual(
            ["event-1", "event-2"], [ident for ident, _ in self.updates],
            "a second digest must not re-release what the first one cleared",
        )
        self.assertEqual([], summary.parked_rows(self.history))

    async def test_release_ignores_another_doctors_parked_card(self) -> None:
        stranger = parked_event("event-9", "not yours")
        stranger.doctor_id = "d2"
        with patch.object(adapters.store, "now", lambda: NOW), \
                patch.object(adapters.store, "update_event", self._update_event):
            cleared = await adapters.release_parked("d1", [stranger])
        self.assertEqual([], cleared)


class TheEndOfDayCardIsUnchangedByDefault(unittest.TestCase):
    def test_no_parked_items_means_the_card_it_always_was(self) -> None:
        counts = summary.Counts(carried=0)
        self.assertEqual(
            summary.card(counts, "Dr Mohamed"),
            summary.card(counts, "Dr Mohamed", parked=()),
        )
        self.assertNotIn(
            summary.PARKED_HEADING,
            "\n".join(summary.card(counts, "Dr Mohamed")["lines"]),
        )

    def test_parked_items_are_appended_when_they_are_handed_in(self) -> None:
        rows = summary.parked_rows([parked_event("event-1", "🧪 Lab results")])
        card = summary.card(
            summary.Counts(carried=1), "Dr Mohamed",
            parked=rows, names={"p1": "Ahmed Ali"},
        )
        self.assertIn(
            f"{summary.PARKED_HEADING} (1):", card["lines"]
        )
        self.assertIn("  · Ahmed Ali: 🧪 Lab results", card["lines"])


# --------------------------------------------------------------------------- #
# S24-F: a Case Steward hold, and the one direction it is allowed to move
# --------------------------------------------------------------------------- #
# The hold is a mark a sender puts on a message it is already sending. It can
# make that message quieter and it can bring a card that was already parked back
# sooner. Every other thing it might be imagined to do is a test below.
HELD_UNTIL = (NOW + timedelta(hours=2)).isoformat()
LATE = (NOW + timedelta(days=5)).isoformat()


def routine_card() -> dict:
    """A card the Coordinator writes: no class on it, so it rings today."""
    return {"title": "Barrier needs you · Ahmed", "severity": "yellow",
            "lines": ["Barrier: cost."], "actions": []}


def held(text: str = "Ahmed's follow-up is blocked.", *, until: object = HELD_UNTIL,
         extra: Optional[dict] = None, card: Optional[dict] = None
         ) -> OutboundMessage:
    meta: dict = {adapters.STEWARD_HOLD: until}
    meta.update(extra or {})
    return OutboundMessage(text=text, patient_id="p1", meta=meta,
                           card=card if card is not None else routine_card())


class AHoldOnlyEverMakesAMessageQuieter(Harness):
    async def test_a_routine_card_is_parked_with_the_holds_own_moment(self
                                                                     ) -> None:
        out, receipt = await self.fan(held(), target=enrolled())

        self.assertEqual(adapters.PARKED, out.phone_routes[0].decision)
        self.assertFalse(out.phone_routes[0].rang_the_phone)
        self.send_card.assert_not_awaited()
        self.assertEqual("event-1", receipt,
                         "the cockpit copy is written exactly as before")

        note = self.phone_note()
        self.assertEqual(summary.PARKED, note["decision"])
        self.assertEqual(HELD_UNTIL, note["release_at"])
        self.assertEqual("steward", note["held_by"])

        rows = summary.parked_rows([_event(self.appended[0])])
        self.assertEqual(1, len(rows))
        self.assertEqual(HELD_UNTIL, rows[0]["release_at"])
        self.assertEqual("steward", rows[0]["held_by"])

    async def test_the_morning_is_the_ceiling_and_a_hold_cannot_reach_past_it(
            self) -> None:
        """A hold that asks for five days gets the morning. Code decides."""
        await self.fan(held(until=LATE), target=enrolled())
        self.assertEqual(timing.next_digest_at(NOW).isoformat(),
                         self.phone_note()["release_at"])

    async def test_an_already_parked_card_can_be_brought_back_sooner(self
                                                                    ) -> None:
        await self.fan(
            held(card=review_card(),
                 extra={"notification_class": "REVIEW_READY"}),
            target=enrolled())
        note = self.phone_note()
        self.assertEqual(HELD_UNTIL, note["release_at"])
        self.assertLess(note["release_at"],
                        timing.next_digest_at(NOW).isoformat())
        self.assertEqual("REVIEW_READY", note["class"])

    # -- the two it may never touch ----------------------------------------- #
    async def test_danger_during_a_hold_still_rings_instantly(self) -> None:
        """The mark is not even read: the push branch answers before it."""
        out, _ = await self.fan(
            held("Critical potassium for Ahmed.", card=None,
                 extra={"notification_class": NotificationClass.DANGER.value}),
            target=enrolled())
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)
        self.assertTrue(out.phone_routes[0].rang_the_phone)
        self.assertEqual({}, self.phone_note(),
                         "a held danger card must be stored exactly as before")

    async def test_urgent_sla_during_a_hold_still_rings(self) -> None:
        out, _ = await self.fan(
            held("Ahmed has not answered the critical callback.", card=None,
                 extra={"notification_class": "URGENT_SLA"}),
            target=enrolled())
        self.send_card.assert_awaited_once()
        self.assertEqual(adapters.PUSHED, out.phone_routes[0].decision)

    async def test_the_hold_is_read_after_the_pushing_branches_in_the_source(
            self) -> None:
        """The ordering above is the rail, so it is asserted as ordering."""
        from pathlib import Path

        source = (Path(adapters.__file__)).read_text(encoding="utf-8")
        body = source.split("async def route_for(", 1)[1]
        self.assertLess(body.index("if known in PUSH_CLASSES:"),
                        body.index("held = held_until(msg)"))

    # -- the two it may never make louder ------------------------------------ #
    async def test_a_hold_never_wakes_a_message_a_sender_marked_quiet(self
                                                                     ) -> None:
        """Parking silent work would put it in a digest. That is louder."""
        for quiet in ("SILENT_WORK", "SOLICITED_RESPONSE"):
            with self.subTest(quiet=quiet):
                self.setUp()
                out, _ = await self.fan(
                    held(card=None, extra={"notification_class": quiet}),
                    target=enrolled())
                self.assertEqual(adapters.SUPPRESSED,
                                 out.phone_routes[0].decision)
                self.assertEqual(summary.SUPPRESSED,
                                 self.phone_note()["decision"])
                self.assertEqual([], summary.parked_rows(
                    [_event(self.appended[0])]))

    async def test_a_hold_never_quiets_a_doctor_who_is_not_enrolled(self
                                                                    ) -> None:
        """Off the cohort a hold is not refused, it is not covered.

        The route says LEGACY rather than PUSHED, which is the more honest of
        the two labels for the same thing and is the label every other quiet
        branch already uses off the cohort. What matters is underneath it and
        is asserted below: the phone rings, every channel runs, and the card is
        stored with exactly the bytes it would have carried with no Steward.
        """
        out, _ = await self.fan(held(), target=enrolled(False))
        self.assertEqual(adapters.LEGACY, out.phone_routes[0].decision)
        self.assertTrue(out.phone_routes[0].push)
        self.assertTrue(out.phone_routes[0].rang_the_phone)
        self.send_card.assert_awaited_once()
        self.assertEqual({}, self.phone_note())

        held_note = self.phone_note()
        self.setUp()
        await self.fan(
            OutboundMessage(text="Ahmed's follow-up is blocked.",
                            patient_id="p1", card=routine_card()),
            target=enrolled(False))
        self.assertEqual(self.phone_note(), held_note,
                         "an unenrolled doctor's card changed under a hold")

    # -- fail open ----------------------------------------------------------- #
    async def test_an_unreadable_hold_routes_as_if_it_were_not_there(self
                                                                    ) -> None:
        for bad in ("", "   ", "tomorrow morning", "the case steward", 5,
                    None, {"at": HELD_UNTIL}, [HELD_UNTIL], True):
            with self.subTest(bad=bad):
                self.setUp()
                out, _ = await self.fan(held(until=bad), target=enrolled())
                self.assertEqual(adapters.PUSHED,
                                 out.phone_routes[0].decision)
                self.send_card.assert_awaited_once()
                self.assertEqual({}, self.phone_note())

    def test_the_reader_never_raises_on_anything_at_all(self) -> None:
        for meta in ({}, {adapters.STEWARD_HOLD: object()}, {"other": 1}):
            with self.subTest(meta=meta):
                self.assertEqual(
                    "", adapters.held_until(
                        OutboundMessage(text="x", meta=meta)))
        self.assertEqual(HELD_UNTIL, adapters.held_until(held()))

    def test_the_clamp_never_raises_and_never_returns_nothing(self) -> None:
        morning = timing.next_digest_at(NOW).isoformat()
        for asked in ("", "not a time", "2026-08-31", HELD_UNTIL, LATE):
            with self.subTest(asked=asked):
                answer = adapters.release_moment(
                    adapters.PhoneRoute(adapters.PARKED, release_at=asked), NOW)
                self.assertTrue(answer)
                self.assertLessEqual(answer, morning)


class AHeldCardIsOwedByExactlyOneMorning(unittest.IsolatedAsyncioTestCase):
    """The digest half: a held card is listed once and then stops being owed."""

    def setUp(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    async def _update_event(self, event_id, **fields):
        self.updates.append((event_id, fields))
        for event in self.history:
            if event.id == event_id:
                event.meta = fields.get("meta", event.meta)

    def held_card(self) -> SimpleNamespace:
        note = {"decision": summary.PARKED, "class": "",
                "release_at": HELD_UNTIL, "held_by": "steward"}
        return SimpleNamespace(
            id="event-1", doctor_id="d1", patient_id="p1", ts=NOW, kind="card",
            text="Barrier needs you", meta={"card": routine_card(),
                                            summary.PHONE_META: note})

    async def test_the_morning_releases_a_held_card_exactly_once(self) -> None:
        self.history = [self.held_card()]
        self.assertEqual(1, len(summary.parked_rows(self.history)))

        cleared: list[list[str]] = []
        for _ in range(2):
            with patch.object(adapters.store, "now", lambda: NOW), \
                    patch.object(adapters.store, "update_event",
                                 self._update_event):
                cleared.append(await adapters.release_parked("d1",
                                                             self.history))
        self.assertEqual([["event-1"], []], cleared)
        self.assertEqual([ident for ident, _ in self.updates], ["event-1"])
        self.assertEqual([], summary.parked_rows(self.history),
                         "a released card is not owed by a second morning")

    async def test_releasing_it_keeps_who_parked_it_on_the_record(self) -> None:
        self.history = [self.held_card()]
        with patch.object(adapters.store, "now", lambda: NOW), \
                patch.object(adapters.store, "update_event",
                             self._update_event):
            await adapters.release_parked("d1", self.history)
        note = summary.phone_note(self.history[0])
        self.assertEqual("steward", note["held_by"])
        self.assertEqual(HELD_UNTIL, note["release_at"])
        self.assertTrue(note["digest_at"])


if __name__ == "__main__":
    unittest.main()
