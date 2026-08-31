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
from datetime import datetime, timezone
from types import SimpleNamespace
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


if __name__ == "__main__":
    unittest.main()
